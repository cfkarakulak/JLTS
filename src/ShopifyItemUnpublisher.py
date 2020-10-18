import asyncio
import aiohttp
import getopt
import bugsnag
import backoff

from loguru import logger
from datetime import datetime
from dynaconf import settings
from utils.Database import Database
from utils.Queue import Queue
from utils.Helpers import Helpers
from utils.Retry import Retry, TooManyRequestsException, ServerConnectionError

# Get DB instance
DB = Database.instance()


class ShopifyItemUnpublisher:

    def __init__(self):
        # Bugsnag for error reporting
        bugsnag.configure(api_key=settings.APP.Bugsnag.Key)

    async def process(self, payload):
        """ Gets called every time when a message is consumed """

        # Reject if not my job
        if payload['task'] != 'Unpublish':
            return logger.critical("{task} | Task belongs to another service, rejecting", task=payload['task'])

        # Get fba item
        self.fba_item = Helpers.get_fba_item(payload['fba_item_id'])
        if not self.fba_item:
            return logger.critical("{fba_item_id} | Fba item id not found", fba_item_id=payload['fba_item_id'])

        # Get the related user
        self.user = Helpers.get_user(self.fba_item['user_id'])
        if not self.user:
            return logger.critical("{user_id} | User not found", user_id=self.fba_item['user_id'])

        # Get Shopify credentials
        self.shopify_creds = self.user['settings'].get('shopify_creds', {})
        if not all([self.shopify_creds, self.shopify_creds.get('token')]):
            return logger.critical("{user_id} | Shopify token not found", user_id=self.user['_id'])

        # Configure logger format
        Helpers.configure_logger(user=self.user['_id'])

        # Prepare product to send
        await self.compose_product()

    async def compose_product(self):
        """ Form shopify product data using the fba item """

        # Get Shopify listing of fba_item
        shopify_listing = DB.ShopifyListings.find_one({
            'fba_item_id': self.fba_item['_id'],
        })

        if not shopify_listing:
            return logger.error("{fba_item_id} | Shopify listing missing", fba_item_id=self.fba_item['_id'])

        # Just setting published_at to a nil value.
        product = {
            'local_fba_item': self.fba_item,
            'local_shopify_item': shopify_listing,
            'remote_shopify_item': {
                'published_at': None,
            },
        }

        # Unpublish product
        await self.unpublish_product(product)

    @backoff.on_exception(backoff.fibo, (TooManyRequestsException, ServerConnectionError), max_tries=15, jitter=None, on_backoff=Retry.log)
    async def unpublish_product(self, product):
        """ Deactivate product on Shopify store asynchronously """

        # Set header
        self.headers = {
            'Content-Type': 'application/json',
            'X-Shopify-Access-Token': self.shopify_creds['token'],
        }

        logger.debug("Making a request to Shopify API")

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=120),
            connector=aiohttp.TCPConnector(limit=5),
            headers=self.headers,
        ) as session:
            try:
                request = await session.put(url=settings.SHOPIFY.Endpoints.Product.format(
                        domain=self.shopify_creds['domain'],
                        product_id=product['local_shopify_item']['shopify_item_id'],
                    ),
                    json={
                        'product': product['remote_shopify_item'],
                    }
                )
            except (
                aiohttp.client_exceptions.ClientConnectorError,
                aiohttp.client_exceptions.ClientOSError,
                aiohttp.client_exceptions.ServerDisconnectedError
            ) as e:
                logger.critical(e)
                raise ServerConnectionError()

            try:
                response = await request.json()
            except aiohttp.client_exceptions.ContentTypeError as e:
                return logger.critical(e)
                raise ServerConnectionError()

            if request.status == 200:
                """ Update FbaItems and ShopifyListings collections with the latest status """

                DB.ShopifyListings.update_one({'shopify_item_id': product['local_shopify_item']['shopify_item_id']}, {
                    '$set': {
                        'published_at': None,
                        'active': False,
                        'updated_at': datetime.utcnow(),
                    },
                })

                DB.FbaItems.update_one({'_id': product['local_fba_item']['_id']}, {
                    '$set': {
                        '_shopify_status': 'UNPUBLISHED',
                        'shopify_publish_status': False,
                        'shopify_item_id': None,
                        'shopify_listing_price': None,
                        'updated_at': datetime.utcnow(),
                    },
                })

                logger.success(
                    "{fba_item_id} | {shopify_item_id} | Unpublished",
                    fba_item_id=product['local_fba_item']['_id'],
                    shopify_item_id=product['local_shopify_item']['shopify_item_id'],
                )

            if request.status == 404:
                """ Shopify returns {error: Not Found} """

                DB.FbaItems.update_one({'_id': product['local_fba_item']['_id']}, {
                    '$set': {
                        '_shopify_status': 'LISTED',
                        'updated_at': datetime.utcnow(),
                    },
                })

                logger.error(
                    "{fba_item_id} | {shopify_item_id} | seems removed from Shopify",
                    fba_item_id=product['local_fba_item']['_id'],
                    shopify_item_id=product['local_shopify_item']['shopify_item_id'],
                )

            if request.status == 429:
                logger.info(
                    "{shopify_item_id} | API rate limit reached, retrying...",
                    shopify_item_id=product['local_shopify_item']['shopify_item_id'],
                )

                raise TooManyRequestsException()

            if request.status not in [200, 404, 429]:
                """ Error is not handled yet """

                DB.FbaItems.update_one({'_id': product['local_fba_item']['_id']}, {
                    '$set': {
                        '_shopify_status': 'LISTED',
                        'updated_at': datetime.utcnow(),
                    },
                })

                logger.warning(
                    "{status_code} Status Code | {shopify_item_id} | {response}",
                    status_code=request.status,
                    shopify_item_id=product['local_shopify_item']['shopify_item_id'],
                    response=response,
                )
