import asyncio
import aio_pika
import aiormq.exceptions
import time
import json
import bugsnag

from loguru import logger
from dynaconf import settings
from utils.Retry import TooManyRequestsException

class Queue:
    """ Queue helper for most common methods """

    def __init__(self, url):
        self.URL = url

        self.running_tasks = 0
        self.last_task_time = None
        self.start_time = time.time()
        self.exclusive_failed = False

    async def connect(self, target, loop):
        self.loop = loop
        self.connection = await aio_pika.connect_robust(url=self.URL, loop=self.loop)
        self.channel = await self.connection.channel()

        self.queue = await self.channel.declare_queue(
            name=target,
            durable=True,
        )

    async def publish(self, routing_key, message):
        message = aio_pika.Message(
            body=message.encode()
        )

        await self.channel.default_exchange.publish(
            routing_key=routing_key,
            message=message,
        )

    async def consume(self, callback):
        try:
            async for message in self.queue.iterator(exclusive=True):
                self.loop.create_task(self.handle_message(message, callback))
        except aiormq.exceptions.ChannelAccessRefused:  # could not get exclusive access
            logger.error("Unable to get exclusive access to queue, shutting down")
            self.exclusive_failed = True
            return
        except RuntimeError:
            pass

    async def handle_message(self, message, callback):
        try:
            def fetch(body):
                """Convert message from queue to a json message"""

                result = {}
                message = json.loads(body)
                if isinstance(message, dict):
                    result = message

                return result

            async with message.process():
                self.last_task_time = time.time()
                self.running_tasks += 1

                try:
                    await callback(fetch(message.body))
                    await asyncio.sleep(0.1)
                finally:
                    self.running_tasks -= 1
        except TooManyRequestsException as e:
            logger.error("Out of retries !")
        except Exception as e:
            logger.exception("Exception processing message")
            bugsnag.notify(e)

    async def should_live(self):
        while True:
            now = time.time()
            time_since_last_task = now - self.last_task_time if self.last_task_time else None

            no_tasks_running = self.running_tasks == 0
            have_run_a_task = time_since_last_task is not None
            enough_time_since_last_task = have_run_a_task and time_since_last_task > 10
            too_long_since_start = not have_run_a_task and (now - self.start_time) > 60

            if no_tasks_running and (enough_time_since_last_task or too_long_since_start or self.exclusive_failed):
                break

            await asyncio.sleep(1)

    async def shutdown(self):
        if self.queue.iterator():
            await self.queue.iterator().close()

        if self.channel:
            await self.channel.close()

        if self.connection:
            await self.connection.close()
