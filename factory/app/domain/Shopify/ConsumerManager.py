import asyncio
import ast
import sys

from subprocess import Popen, PIPE, CalledProcessError
from dynaconf import settings
from pathlib import Path
from utils.Queue import Queue


def handle(channel, method, properties, body):
    """ Spin up corresponding consumer for a specific task """

    payload = Queue.fetch(body)

    queue = payload["target"]

    def spin_up_consumer():
        path = Path(__file__).parent.absolute()

        command = [
            f'/app/venv/bin/python',
            f'{path}/consumers/ShopifyItemLister.py',
            f'--target={queue}'
        ]

        with Popen(command, stdout=sys.stdout, stderr=sys.stderr, bufsize=1, universal_newlines=True) as p:
            for line in p.stdout:
                print(line, end='')

            if p.returncode != 0:
                raise CalledProcessError(p.returncode, p.args)

    spin_up_consumer()

host, connection, channel = Queue.setup(
    host=settings.QUEUE.RabbitMQ.Host,
)

Queue.connect(
    channel=channel,
    queue=settings.DOMAIN.Shopify.Queue.PendingTasks,
)

channel.basic_consume(
    queue=settings.DOMAIN.Shopify.Queue.PendingTasks,
    on_message_callback=handle,
    auto_ack=True
)

channel.start_consuming()
