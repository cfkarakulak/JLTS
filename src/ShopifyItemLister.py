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


class ShopifyItemLister:

    def __init__(self):
        # Bugsnag for error reporting
        bugsnag.configure(api_key=settings.APP.Bugsnag.Key)

    async def process(self, payload):
        """ Gets called every time when a message is consumed """

        # Reject if not my job
        if payload['task'] != 'List':
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

        amazon_price = Helpers.get_amazon_price(self.fba_item)

        if amazon_price is None or amazon_price <= 0:
            return logger.error("{fba_item_id} | Amazon price must be greater than 0", fba_item_id=self.fba_item['_id'])

        fba_item_title = self.fba_item['title']
        fba_item_description = Helpers.get_product_description(self.fba_item, subtract_ebay_footer=True)
        fba_item_sku = self.fba_item['seller_sku']
        fba_item_images = [{'src': image} for image in Helpers.get_edited_or_default(self.fba_item, 'images') or []]
        fba_item_price = amazon_price / 100.0
        fba_item_quantity = Helpers.get_product_quantity(self.fba_item)

        is_published = False
        if Helpers.get_shopify_options(self.user['settings'], 'default_product_import_state', 'visible'):
            is_published = True

        shopify_sale_price = Helpers.get_price_by_formula(
            item_price=fba_item_price,
            formula=self.user['settings'].get('shopify_pricing_formulas', {}),
            price_type='sale_price',
        )

        shopify_compare_at_price = Helpers.get_price_by_formula(
            item_price=shopify_sale_price,
            formula=self.user['settings'].get('shopify_pricing_formulas', {}),
            price_type='compare_at_price',
        )

        # Shopify is the one to save on user's Shopify store
        # Amazon is for inserting an entry into our Database (seller_sku, user_id...)
        product = {
            'local_fba_item': self.fba_item,
            'remote_shopify_item': {
                'title': fba_item_title,
                'body_html': fba_item_description,
                'images': fba_item_images,
                'published': is_published,
                'variants': [{
                    'price': shopify_sale_price,
                    'compare_at_price': shopify_compare_at_price,
                    'inventory_quantity': fba_item_quantity,
                    'inventory_management': 'shopify',
                    'sku': fba_item_sku,
                    'taxable': True,
                    'requires_shipping': True,
                }],
            },
        }

        # List product on Shopify
        # published = true | false
        await self.list_product(product)

    @backoff.on_exception(backoff.fibo, (TooManyRequestsException, ServerConnectionError), max_tries=15, jitter=None, on_backoff=Retry.log)
    async def list_product(self, product):
        """ Publish products on Shopify store asynchronously """

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
                request = await session.post(
                    url=settings.SHOPIFY.Endpoints.Products.format(
                        domain=self.shopify_creds['domain'],
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

            if request.status == 201:
                try:
                    """ Insert entry in db based on calculated values above """
                    document = DB.ShopifyListings.insert_one({
                        '_id': Helpers.generate_mongo_id(),
                        'title': response['product']['title'],
                        'shopify_item_id': response['product']['id'],
                        'price': Helpers.format_price(float(response['product']['variants'][0]['price']) * 100, precision=0),
                        'compare_at_price': Helpers.format_price(float(response['product']['variants'][0]['compare_at_price']) * 100, precision=0),
                        'quantity': response['product']['variants'][0]['inventory_quantity'],
                        'thumb': (response['product']['image'] if response['product'].get('image', {}) else {}).get('src') or None,
                        'fba_item_id': product['local_fba_item']['_id'],
                        'seller_sku': product['local_fba_item']['seller_sku'],
                        'asin': product['local_fba_item']['asin'],
                        'user_id': product['local_fba_item']['user_id'],
                        'handle': response['product']['handle'],
                        'published_at': response['product']['published_at'],
                        '_created_at': datetime.utcnow(),
                        'sold': 0,
                        'active': True if response['product']['published_at'] else False,
                    })

                    logger.success(
                        "{fba_item_id} | Listed with the ID of {shopify_item_id}",
                        fba_item_id=product['local_fba_item']['_id'],
                        shopify_item_id=response['product']['id'],
                    )

                    # Means listed as unpublished
                    if response['product']['published_at']:
                        fba_data = {
                            '_shopify_status': 'LISTED',
                            'shopify_item_id': str(response['product']['id']),
                            'shopify_listing_price': Helpers.format_price(float(response['product']['variants'][0]['price']) * 100, precision=0),
                            'shopify_publish_status': True,
                        }

                    if not response['product']['published_at']:
                        fba_data = {
                            '_shopify_status': 'UNPUBLISHED',
                            'shopify_publish_status': False,
                        }

                    DB.FbaItems.update_one({'_id': product['local_fba_item']['_id']}, {
                        '$set': {**fba_data, **{
                                'updated_at': datetime.utcnow(),
                            },
                        },
                    })
                except Exception as e:
                    logger.error(e)

                    DB.FbaItems.update_one({'_id': product['local_fba_item']['_id']}, {
                        '$set': {
                            '_shopify_status': 'FAILED_TO_LIST',
                            'updated_at': datetime.utcnow(),
                        },
                    })

            if request.status == 429:
                logger.info(
                    "{fba_item_id} | API rate limit reached, retrying...",
                    fba_item_id=product['local_fba_item']['_id'],
                )

                raise TooManyRequestsException()

            if request.status not in [201, 429]:
                logger.warning(
                    "{status_code} Status Code | {fba_item_id} | {response}",
                    status_code=request.status,
                    fba_item_id=product['local_fba_item']['_id'],
                    response=response,
                )
