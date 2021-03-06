import os
import argparse
import random
from time import time
from time import sleep
from sqlalchemy import text
from sqlalchemy import orm

from app import db
from app import logger

from queue_main import DbQueue
from pub import Pub
from util import run_sql
from util import elapsed
from util import clean_doi



class DbQueuePub(DbQueue):
    def table_name(self, job_type):
        table_name = "pub"
        return table_name

    def process_name(self, job_type):
        if self.parsed_vars:
            process_name = self.parsed_vars.get("method")
        return process_name

    def worker_run(self, **kwargs):
        single_obj_id = kwargs.get("id", None)
        chunk = kwargs.get("chunk", 100)
        limit = kwargs.get("limit", 10)
        run_class = Pub
        run_method = kwargs.get("method")

        if single_obj_id:
            limit = 1
            queue_table = None
        elif run_method=="refresh":
            queue_table = "pub"
            if not limit:
                limit = 1000
            text_query_pattern = """WITH refresh_pub_queue AS (
                   SELECT id
                   FROM   {queue_table}
                   WHERE  started is null
                   -- AND scrape_updated is null
                   -- order by rand desc
                   order by scrape_updated asc
                   LIMIT  {chunk}
                   FOR UPDATE SKIP LOCKED
                   )
                UPDATE {queue_table} queue_rows_to_update
                SET    started=now()
                FROM   refresh_pub_queue
                WHERE refresh_pub_queue.id = queue_rows_to_update.id
                RETURNING refresh_pub_queue.id;"""
            text_query = text_query_pattern.format(
                limit=limit,
                chunk=chunk,
                queue_table=queue_table
            )
            logger.info(u"the queue query is:\n{}".format(text_query))
        else:
            queue_table = "pub_queue"
            if not limit:
                limit = 1000
            text_query_pattern = """WITH update_pub_queue AS (
                       SELECT id
                       FROM   {queue_table}
                       WHERE  started is null
                       order by finished asc
                       nulls first
                   LIMIT  {chunk}
                   FOR UPDATE SKIP LOCKED
                   )
                UPDATE {queue_table} queue_rows_to_update
                SET    started=now()
                FROM   update_pub_queue
                WHERE update_pub_queue.id = queue_rows_to_update.id
                RETURNING update_pub_queue.id;"""
            text_query = text_query_pattern.format(
                limit=limit,
                chunk=chunk,
                queue_table=queue_table
            )
            logger.info(u"the queue query is:\n{}".format(text_query))
        index = 0
        start_time = time()
        while True:
            new_loop_start_time = time()
            if single_obj_id:
                single_obj_id = clean_doi(single_obj_id)
                objects = [run_class.query.filter(run_class.id == single_obj_id).first()]
            else:
                logger.info(u"looking for new jobs")

                job_time = time()
                row_list = db.engine.execute(text(text_query).execution_options(autocommit=True)).fetchall()
                object_ids = [row[0] for row in row_list]
                logger.info(u"got ids, took {} seconds".format(elapsed(job_time)))

                job_time = time()
                q = db.session.query(Pub).options(orm.undefer('*')).filter(Pub.id.in_(object_ids))
                objects = q.all()
                logger.info(u"got pub objects in {} seconds".format(elapsed(job_time)))

                # shuffle them or they sort by doi order
                random.shuffle(objects)

                # objects = Pub.query.from_statement(text(text_query)).execution_options(autocommit=True).all()

                # objects = run_class.query.from_statement(text(text_query)).execution_options(autocommit=True).all()
                # id_rows =  db.engine.execute(text(text_query)).fetchall()
                # ids = [row[0] for row in id_rows]
                #
                # job_time = time()
                # objects = run_class.query.filter(run_class.id.in_(ids)).all()

                # logger.info(u"finished get-new-objects query in {} seconds".format(elapsed(job_time)))


            if not objects:
                # logger.info(u"sleeping for 5 seconds, then going again")
                sleep(5)
                continue

            object_ids = [obj.id for obj in objects]
            self.update_fn(run_class, run_method, objects, index=index)

            # logger.info(u"finished update_fn")
            if queue_table:
                object_ids_str = u",".join([u"'{}'".format(id.replace(u"'", u"''")) for id in object_ids])
                object_ids_str = object_ids_str.replace(u"%", u"%%")  #sql escaping
                sql_command = u"update {queue_table} set finished=now(), started=null where id in ({ids})".format(
                    queue_table=queue_table, ids=object_ids_str)
                # logger.info(u"sql command to update finished is: {}".format(sql_command))
                run_sql(db, sql_command)
                # logger.info(u"finished run_sql")

            # finished is set in update_fn
            index += 1
            if single_obj_id:
                return
            else:
                self.print_update(new_loop_start_time, chunk, limit, start_time, index)




if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run stuff.")
    parser.add_argument('--id', nargs="?", type=str, help="id of the one thing you want to update (case sensitive)")
    parser.add_argument('--doi', nargs="?", type=str, help="id of the one thing you want to update (case insensitive)")
    parser.add_argument('--method', nargs="?", type=str, default="update", help="method name to run")

    parser.add_argument('--reset', default=False, action='store_true', help="do you want to just reset?")
    parser.add_argument('--run', default=False, action='store_true', help="to run the queue")
    parser.add_argument('--status', default=False, action='store_true', help="to logger.info(the status")
    parser.add_argument('--dynos', default=None, type=int, help="scale to this many dynos")
    parser.add_argument('--logs', default=False, action='store_true', help="logger.info(out logs")
    parser.add_argument('--monitor', default=False, action='store_true', help="monitor till done, then turn off dynos")
    parser.add_argument('--kick', default=False, action='store_true', help="put started but unfinished dois back to unstarted so they are retried")
    parser.add_argument('--limit', "-l", nargs="?", type=int, help="how many jobs to do")
    parser.add_argument('--chunk', "-ch", nargs="?", default=500, type=int, help="how many to take off db at once")

    parsed_args = parser.parse_args()

    job_type = "normal"  #should be an object attribute
    my_queue = DbQueuePub()
    my_queue.parsed_vars = vars(parsed_args)
    my_queue.run_right_thing(parsed_args, job_type)
