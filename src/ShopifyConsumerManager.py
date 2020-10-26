import asyncio
import bugsnag
import psutil

from loguru import logger
from subprocess import Popen
from dynaconf import settings
from pathlib import Path

import json
import pika
import time


class ShopifyConsumerManager:
    # GPL prevents creating exceeding processes and keeps CPU at a sane level
    # GLOBAL_PROCESS_LIMIT / CONSUMER_PER_QUEUE gives you the number of JL customers
    # that this machine can process simultaneously
    GLOBAL_PROCESS_LIMIT = settings.APP.ConsumerManager.GlobalProcessLimit

    # Multiple consuming of queues will be avoided even though this is higher than one.
    # Exclusive flag must be turned off at utils/Queue.py:45
    CONSUMER_PER_QUEUE = settings.APP.ConsumerManager.ConsumerPerQueue

    def handle(self, channel, method, properties, body):
        """ Spin up corresponding consumer for a specific task """

        def fetch(body):
            """Convert message from queue to a json message"""

            result = {}
            message = json.loads(body)
            if isinstance(message, dict):
                result = message

            return result

        # Get the message
        payload = fetch(body)

        # Determine the target queue name
        queue = payload["target"]

        def spin_up_consumer():
            path = Path(__file__).parent.absolute()

            command = [
                f'python3.8',  # f'{path}/venv/bin/python'
                f'{path}/ShopifyDispatcher.py',
                f'--target={queue}'
            ]

            return Popen(command)

        try:
            # Find all processes that are running other than
            # the children of this (current) one.
            all_running_processes = [
                x for x in psutil.Process().children()
                if all([x.status() == 'running', f'--target={queue}' not in (' '.join(x.cmdline()))])
            ]

            if len(all_running_processes) + 1 > self.GLOBAL_PROCESS_LIMIT:
                logger.warning(
                    "Working at the maximum capacity, {count} processes running, will try in 5 secs.",
                    count=len(all_running_processes)
                )

                # Avoid hammering the machine
                time.sleep(5)

                # Pretend didn't see that.
                return channel.basic_reject(method.delivery_tag, requeue=True)

            # Find all processes assigned to the same queue
            child_processes = [x for x in psutil.Process().children() if f'--target={queue}' in (' '.join(x.cmdline()))]

            # There already exists {count} process(es) running for this queue
            if len(child_processes) < self.CONSUMER_PER_QUEUE:
                spin_up_consumer()

        # calling cmdline() on a zombie proc will throw this exception
        except psutil.ZombieProcess:
            # since actual queue connects to amqp with blocking connection
            # it will remain as a zombie, so we wait and drop it just for the sake of cleanup
            for x in psutil.Process().children():
                if x.status() == 'zombie':
                    x.wait()

            spin_up_consumer()
        except psutil.NoSuchProcess:
            spin_up_consumer()

        # Finally acknowledge the message
        channel.basic_ack(method.delivery_tag)

def main():
    # Bugsnag for error reporting
    bugsnag.configure(api_key=settings.APP.Bugsnag.Key)

    # Set up AMQP connection
    try:
        connection = pika.BlockingConnection(
            pika.URLParameters(settings.QUEUE.RabbitMQ.URL)
        )
    except pika.exceptions.AMQPConnectionError:
        return logger.error("Can't establish AMQP connection")

    shopify_consumer_manager = ShopifyConsumerManager()

    # Declare queue
    channel = connection.channel()
    channel.queue_declare(queue=settings.SHOPIFY.Queue.PendingTasks, durable=True)

    channel.basic_consume(
        queue=settings.SHOPIFY.Queue.PendingTasks,
        on_message_callback=shopify_consumer_manager.handle,
        auto_ack=False,
    )

    channel.start_consuming()

if __name__ == "__main__":
    logger.critical("BU yyYyYYyYyyyyYYyY")
    # main()
