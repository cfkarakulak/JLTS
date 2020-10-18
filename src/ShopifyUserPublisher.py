import asyncio
import getopt
import sys
import bugsnag
import json

from loguru import logger
from dynaconf import settings
from utils.Database import Database
from utils.Queue import Queue

# Get DB instance
DB = Database.instance()


class ShopifyUserPublisher:

    def publish(self, task):
        queue = Queue(url=settings.QUEUE.RabbitMQ.URL)

        # Get event loop
        loop = asyncio.get_event_loop()

        # Get users with Shopify token
        users = DB.users.find({
            'settings.shopify_creds.token': {'$exists': True},
        })

        for user in users:
            try:
                shopify_domain = user['settings']['shopify_creds'].get('domain', '').split('.myshopify.com')[0]
            except Exception as e:
                logger.critical("{user_id} | Shopify domain not found", user_id=user['_id'])
                continue

            # Publish first message to actual task queue
            target_queue = f"DEBUG::Shopify.{user['_id']}.{shopify_domain}"
            target_queue_message = {'task': task, 'user_id': user['_id']}

            loop.run_until_complete(queue.connect(target=target_queue, loop=loop))
            loop.create_task(queue.publish(routing_key=target_queue, message=json.dumps(target_queue_message)))

            # Publish second message to pending tasks queue
            pending_tasks_queue = settings.SHOPIFY.Queue.PendingTasks
            pending_tasks_queue_message = {'task': task, 'target': target_queue}

            loop.run_until_complete(queue.connect(target=pending_tasks_queue, loop=loop))
            loop.create_task(queue.publish(routing_key=pending_tasks_queue, message=json.dumps(pending_tasks_queue_message)))

        loop.run_until_complete(queue.shutdown())

        # Close the loop
        loop.close()


def main(task):
    try:
        file = settings.APP.Tasks[task]
    except Exception as e:
        return logger.critical(f"{e} | Task is undefined")

    # Initiate ShopifyUserPublisher
    shopify_user_publisher = ShopifyUserPublisher()

    # Publish to queues
    shopify_user_publisher.publish(task)

if __name__ == "__main__":
    # Bugsnag for error reporting
    bugsnag.configure(api_key=settings.APP.Bugsnag.Key)

    try:
        opts, args = getopt.getopt(sys.argv[1:], 'hm:d', ['help', 'task='])
    except getopt.GetoptError as err:
        print(str(err))

    for o, a in opts:
        if o in ("-t", "--task"):
            task = a
        else:
            assert False, "Unhandled option"

    main(task=task)
