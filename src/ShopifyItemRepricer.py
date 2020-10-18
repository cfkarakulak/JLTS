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


class ShopifyItemRepricer:

    def __init__(self):
        # Bugsnag for error reporting
        bugsnag.configure(api_key=settings.APP.Bugsnag.Key)

    async def process(self, payload):
        """ Gets called every time when a message is consumed """

        # Reject if not my job
        if payload['task'] != 'Reprice':
            return logger.critical("{task} | Task belongs to another service, rejecting", task=payload['task'])

        # Get the related user
        self.user = Helpers.get_user(payload['user_id'])
        if not self.user:
            return logger.critical("{user_id} | User not found", user_id=payload['user_id'])

        # Get Shopify credentials
        self.shopify_creds = self.user['settings'].get('shopify_creds', {})
        if not all([self.shopify_creds, self.shopify_creds.get('token')]):
            return logger.critical("{user_id} | Shopify token not found", user_id=self.user['_id'])

        # Check if the service enabled
        if Helpers.get_shopify_options(self.user['settings'], 'automatic_pricing_sync', 'off'):
            return logger.debug("{user_id} | Pricing service disabled by the user", user_id=payload['user_id'])

        # Configure logger format
        Helpers.configure_logger(user=self.user['_id'])

        # Collect products to revise
        await self.iterate_products()

    async def iterate_products(self):
        """ Find Shopify listing and it's fba item equivalent """
        """ Compare their prices and revise product on Shopify if any change needed """

        # Get all non-delisted listings of the user
        shopify_listings_count = DB.ShopifyListings.count_documents({
            'user_id': self.user['_id'],
            'active': True,
        })

        if not shopify_listings_count:
            return logger.debug("No listing found")

        # Print out start info
        logger.debug("{total} listing(s) found, starting...", total=shopify_listings_count)

        shopify_listings = DB.ShopifyListings.find({
            'user_id': self.user['_id'],
            'active': True,
        })

        for shopify_item in shopify_listings:
            # Find the fba_item equivalent
            fba_item = DB.FbaItems.find_one({
                '_id': shopify_item['fba_item_id'],
            })

            # Fba item is somehow absent
            if not fba_item:
                logger.error("Fba item not found for {fba_item_id}.", fba_item_id=shopify_item['fba_item_id'])
                continue

            price_update = await self.compare_prices(fba_item, shopify_item)
            quantity_update = await self.compare_quantities(fba_item, shopify_item)

            needs_updating = {
                **(price_update if price_update is not None else {}),
                **(quantity_update if quantity_update is not None else {}),
            }

            # Neither price nor quantity needs update
            if not needs_updating:
                # logger.debug("{fba_item_id} | Nothing to update here", fba_item_id=fba_item['_id'])
                continue

            # Should provide the current values to prevent deleting other attrs
            needs_updating = {**{
                'price': int(shopify_item['price']) / 100.0,
                'compare_at_price': int(shopify_item['compare_at_price']) / 100.0,
                'inventory_quantity': int(shopify_item['quantity'])
            }, **needs_updating}

            # Remote is the one to save on user's Shopify store
            # Local is for inserting an entry into our Database (seller_sku, user_id...)
            product = {
                'local_shopify_item': shopify_item,
                'local_fba_item': fba_item,
                'remote_shopify_item': {
                    'id': shopify_item['shopify_item_id'],
                    'variants': [needs_updating],
                },
            }

            # send the product to update
            await self.revise_product(product)

    async def compare_prices(self, fba_item, shopify_item):
        """ Check if price needs updating """

        # Needs to be active and have the pricing info ready
        if not all([fba_item.get('pricing_info'), shopify_item.get('active')]):
            return None

        amazon_price = Helpers.get_amazon_price(fba_item)

        if amazon_price is None or amazon_price <= 0:
            logger.error("{fba_item_id} | Amazon price is missing", fba_item_id=fba_item['_id'])
            return None

        fba_item_price = amazon_price / 100.0

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

        if not all([
            Helpers.format_price(float(shopify_sale_price) * 100, precision=0) == shopify_item['price'],
            Helpers.format_price(float(shopify_compare_at_price) * 100, precision=0) == shopify_item['compare_at_price']
        ]):
            return {
                'price': shopify_sale_price,
                'compare_at_price': shopify_compare_at_price,
            }

    async def compare_quantities(self, fba_item, shopify_item):
        """ Check if quantity needs updating """

        # Needs to be active
        if not shopify_item.get('active'):
            return None

        fba_item_quantity = Helpers.get_product_quantity(fba_item)

        # if not fba_item_quantity:
        #     logger.error("{fba_item_id} | Product quantity is missing", fba_item_id=fba_item['_id'])
        #     return None

        if int(fba_item_quantity) != int(shopify_item['quantity']):
            return {
                'inventory_quantity': fba_item_quantity,
            }

    @backoff.on_exception(backoff.fibo, (TooManyRequestsException, ServerConnectionError), max_tries=15, jitter=None, on_backoff=Retry.log)
    async def revise_product(self, product):
        """ Revise products on Shopify store asynchronously """

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
                request = await session.put(
                    url=settings.SHOPIFY.Endpoints.Product.format(
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
                """ Update both collections """

                DB.ShopifyListings.update_one({'shopify_item_id': product['local_shopify_item']['shopify_item_id']}, {
                    '$set': {
                        'updated_at': datetime.utcnow(),
                        'price': Helpers.format_price(float(response['product']['variants'][0]['price']) * 100, precision=0),
                        'compare_at_price': Helpers.format_price(float(response['product']['variants'][0]['compare_at_price']) * 100, precision=0),
                        'quantity': response['product']['variants'][0]['inventory_quantity'],
                    },
                })

                # Update shopify_listing_price on FbaItems
                DB.FbaItems.update_one({'_id': product['local_fba_item']['_id']}, {
                    '$set': {
                        'updated_at': datetime.utcnow(),
                        'shopify_listing_price': Helpers.format_price(float(response['product']['variants'][0]['price']) * 100, precision=0),
                    },
                })

                logger.success(
                    "{fba_item_id} | {shopify_item_id} | Updated with {data}",
                    fba_item_id=product['local_fba_item']['_id'],
                    shopify_item_id=product['local_shopify_item']['shopify_item_id'],
                    data=product['remote_shopify_item']['variants'],
                )

            if request.status == 404:
                """ Shopify returns {error: Not Found} """

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

                logger.warning(
                    "{status_code} Status Code | {shopify_item_id} | {response}",
                    status_code=request.status,
                    shopify_item_id=product['local_shopify_item']['shopify_item_id'],
                    response=response,
                )

