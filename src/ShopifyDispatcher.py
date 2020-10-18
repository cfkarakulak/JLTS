import asyncio
import getopt
import sys
import bugsnag

from loguru import logger
from dynaconf import settings
from utils.Queue import Queue

from ShopifyItemLister import ShopifyItemLister
from ShopifyItemRepricer import ShopifyItemRepricer
from ShopifyItemPublisher import ShopifyItemPublisher
from ShopifyItemUnpublisher import ShopifyItemUnpublisher
from ShopifyOrderFulfiller import ShopifyOrderFulfiller
from ShopifyOrderTracker import ShopifyOrderTracker


def main(target):
    # Initiate Shopify services
    services = {
        'List': ShopifyItemLister(),
        'Publish': ShopifyItemPublisher(),
        'Unpublish': ShopifyItemUnpublisher(),
        'Reprice': ShopifyItemRepricer(),
        'Fulfill': ShopifyOrderFulfiller(),
        'Track': ShopifyOrderTracker(),
    }

    def callback(payload):
        task = payload.get('task')
        service = services.get(task)

        if not task or not service:
            return logger.info("{task} | Task not supported")

        return service.process(payload)

    # Set up AMQP connection
    queue = Queue(url=settings.QUEUE.RabbitMQ.URL)

    # Get event loop
    loop = asyncio.get_event_loop()

    try:
        # Start consuming the queue and wait for jobs to finish
        loop.run_until_complete(queue.connect(target=target, loop=loop))
        loop.create_task(queue.consume(callback=callback))
        loop.run_until_complete(queue.should_live())
        loop.run_until_complete(queue.shutdown())
        loop.run_until_complete(loop.shutdown_asyncgens())
    except ConnectionError as e:
        logger.critical(e)

    # Close the loop
    loop.close()

if __name__ == "__main__":
    # Bugsnag for error reporting
    bugsnag.configure(api_key=settings.APP.Bugsnag.Key)

    try:
        opts, args = getopt.getopt(sys.argv[1:], 'hm:d', ['help', 'target='])
    except getopt.GetoptError as err:
        print(str(err))

    for o, a in opts:
        if o in ("-t", "--target"):
            target = a
        else:
            assert False, "Unhandled option"

    main(target=target)
