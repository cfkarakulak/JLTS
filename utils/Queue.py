import json
import pika

from dynaconf import settings


class Queue:
    """ Queue helper for most common methods """

    def setup(**kwargs):
        host = kwargs.get('host')

        connection = pika.BlockingConnection(
            pika.ConnectionParameters(
                host,
                settings.QUEUE.RabbitMQ.Port,
                settings.DOMAIN.Shopify.Queue.VirtualHost,
                pika.PlainCredentials(
                    str(settings.QUEUE.RabbitMQ.Username),
                    str(settings.QUEUE.RabbitMQ.Password)
                )
            )
        )

        channel = connection.channel()

        return host, connection, channel

    @staticmethod
    def fetch(body):
        """Convert message from queue to a json message"""

        result = {}
        message = json.loads(body)
        if isinstance(message, dict):
            result = message

        return result

    def connect(channel, queue):
        return channel.queue_declare(queue=queue, durable=True)

    @staticmethod
    def name(content, pairs):
        [content := content.replace(str(a), str(b)) for a, b in pairs]
        return content
