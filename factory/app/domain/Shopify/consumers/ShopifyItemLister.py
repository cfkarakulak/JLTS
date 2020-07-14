import ast
import asyncio
import aiohttp
import getopt
import sys
import datetime

from datetime import datetime
from dynaconf import settings
from utils.API import API
from utils.Database import Database
from utils.Queue import Queue
from utils.Helpers import Helpers


class ShopifyItemLister(API):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    async def produce(stack):
        DB = Database.instance()

        for method_frame, properties, body in channel.consume(target):
            payload = Queue.fetch(body)

            # Fetch the listing by id
            fba_item = DB.FbaItems.find_one({
                '_id': payload['fba_item_id'],
            })

            fba_item_title = fba_item['title']
            fba_item_description = Helpers.get_product_description(fba_item, 'x')
            fba_item_images = [{'src': image} for image in Helpers.get_edited_or_default(fba_item, 'images', 'x') or []]
            fba_item_price = 100
            fba_item_quantity = fba_item.get('amazon_quantity', 0)
            fba_item_sku = fba_item.get('seller_sku')

            # Prepare data to list
            product = {
                'title': fba_item_title,
                'body_html': fba_item_description,
                'images': fba_item_images,
                "variants": [{
                    "price": fba_item_price,
                    "inventory_quantity": fba_item_quantity,
                    "inventory_management": "shopify",
                    "sku": fba_item_sku,
                    "taxable": True,
                    "requires_shipping": True,
                }],
            }

            # Insert entry in db based on calculated values above
            # DB.ShopifyListings.insert({
            #     '_created_at': datetime.utcnow(),
            #     'shopify_item_id': str(shopify_item_id),
            #     'seller_sku': seller_sku,
            #     'asin': asin,
            #     'title': etitle,
            #     'active': True,
            #     'quantity': quantity,
            #     'user_id': self.user['_id'],
            # })

            stack.put_nowait(product)
            await asyncio.sleep(0.1)

            # Acknowledge the message
            channel.basic_ack(method_frame.delivery_tag)

    @staticmethod
    async def consume(stack):
        # Extract the listing
        item = await stack.get()

        # Publish it
        await ShopifyItemLister(endpoint='products.json').publish_product(item)

    async def publish_product(self, product):
        """ Publish products on Shopify store asynchronously """

        print(f"Start Worker for: {product}")

        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=30), headers=self.headers) as session:
            response = await session.post(self.url, json={"product": product})
            # print(response.status)


def main():
    # Let's call it 'stack' for now instead of 'queue'
    stack = asyncio.Queue()

    # Produce and consume simultaneously
    loop = asyncio.get_event_loop()
    loop.run_until_complete(
        asyncio.gather(
            ShopifyItemLister.produce(stack),
            asyncio.gather(*[ShopifyItemLister.consume(stack) for _ in range(30)])
        )
    )

    loop.close()


if __name__ == "__main__":
    try:
        opts, args = getopt.getopt(sys.argv[1:], 'hm:d', ['help', 'target='])
    except getopt.GetoptError as err:
        print(str(err))

    for o, a in opts:
        if o in ("-t", "--target"):
            target = a
        else:
            assert False, "Unhandled option"

    # Set up AMQP connection
    host, connection, channel = Queue.setup(
        host=settings.QUEUE.RabbitMQ.Host,
    )

    # Declare queue
    Queue.connect(channel=channel, queue=target)

    main()
