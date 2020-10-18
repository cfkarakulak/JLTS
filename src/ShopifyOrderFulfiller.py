import asyncio
import aiohttp
import getopt
import bugsnag
import backoff
import mws
import xmltodict

from loguru import logger
from datetime import datetime
from dynaconf import settings
from utils.Database import Database
from utils.Queue import Queue
from utils.Helpers import Helpers
from utils.Email import Email
from utils.Retry import Retry, ServerConnectionError

# Get DB instance
DB = Database.instance()


class ShopifyOrderFulfiller:

    def __init__(self):
        # Bugsnag for error reporting
        bugsnag.configure(api_key=settings.APP.Bugsnag.Key)

    async def process(self, payload):
        """ Gets called every time when a message is consumed """

        # Reject if not my job
        if payload['task'] != 'Fulfill':
            return logger.critical("{task} | Task belongs to another service, rejecting", task=payload['task'])

        # Get the related user
        self.user = Helpers.get_user(payload['user_id'])
        if not self.user:
            return logger.critical("{user_id} | User not found", user_id=payload['user_id'])

        # Get Shopify credentials
        self.shopify_creds = self.user['settings'].get('shopify_creds', {})
        if not all([self.shopify_creds, self.shopify_creds.get('token')]):
            return logger.critical("{user_id} | Shopify token not found", user_id=self.user['_id'])

        # Get MWS credentials
        self.mws_creds = self.user['settings'].get('mws_creds', {})
        if not all([self.mws_creds, self.mws_creds.get('mws_merchant_id')]):
            return logger.critical("{user_id} | MWS credentials not found", user_id=self.user['_id'])

        # Check if the service enabled
        if Helpers.get_shopify_options(self.user['settings'], 'automatic_fulfillment', 'off'):
            return logger.debug("{user_id} | Fulfillment service disabled by the user", user_id=payload['user_id'])

        # Configure logger format
        Helpers.configure_logger(user=self.user['_id'])

        # Fetch latest sales
        await self.get_transactions()

    @backoff.on_exception(backoff.fibo, ServerConnectionError, max_tries=15, jitter=None, on_backoff=Retry.log)
    async def get_transactions(self):
        """ Get Shopify transactions of current user """

        # Set header
        self.headers = {
            'Content-Type': 'application/json',
            'X-Shopify-Access-Token': self.shopify_creds['token'],
        }

        logger.debug("Fetching orders from Shopify API")

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=120),
            connector=aiohttp.TCPConnector(limit=5),
            headers=self.headers,
        ) as session:
            try:
                request = await session.get(
                    url=settings.SHOPIFY.Endpoints.Orders.format(
                        domain=self.shopify_creds['domain'],
                    )
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
                """ Process Shopify transactions """
                await self.iterate_orders(response['orders'])

            if request.status != 200:
                logger.error(
                    "{status_code} status code returned, dont know how to act",
                    status_code=request.status,
                )

    async def iterate_orders(self, orders_from_shopify):
        """ Get Shopify transactions of current user """

        orders_from_db = DB.ShopifySales.find({
            'user_id': self.user['_id'],
        })

        if not all([orders_from_db, orders_from_shopify]):
            return logger.info("No transaction found")

        for order_from_db in orders_from_db:
            if order_from_db.get('ignore_fulfilling'):
                logger.debug("{order_id} | Ignore flag seen for this order", order_id=int(order_from_db['order_id']))
                continue

            if order_from_db.get('permanent_fulfillment_error'):
                logger.debug("{order_id} | Permanent fulfillment error seen for this order", order_id=int(order_from_db['order_id']))
                continue

            if order_from_db.get('fulfilled_at'):
                logger.debug("{order_id} | Already fulfilled", order_id=int(order_from_db['order_id']))
                continue

            if order_from_db['financial_status'] != 'paid':
                logger.debug("{order_id} | Financial status is not paid", order_id=int(order_from_db['order_id']))
                continue

            if not order_from_db.get('customer'):
                logger.debug("{order_id} | Purchaser not found", order_id=int(order_from_db['order_id']))
                continue

            order_from_shopify = [x for x in orders_from_shopify if x['id'] == int(order_from_db['order_id'])]

            if not order_from_shopify:
                logger.warning("Order does not exist on Shopify")
                continue

            await self.iterate_order_products(
                order_from_db=order_from_db,
                order_from_shopify=order_from_shopify[0],
            )

    async def iterate_order_products(self, order_from_db, order_from_shopify):
        """ Iterate sold products and prepare fulfillment request """

        if not (shopify_shop_settings := await Helpers.fetch_shopify_settings(
            domain=self.shopify_creds['domain'],
            headers=self.headers,
        )):
            return logger.error("Error when getting store settings from Shopify")

        # Helper for updating the current order product
        def update_order_product(params):
            DB.ShopifySales.update_one({'_id': order_from_db['_id']}, {
                '$set': params,
            })

        amazon_bought_items = []
        for key, sold_product in enumerate(order_from_db['products']):
            # Get related Shopify listing
            shopify_item = DB.ShopifyListings.find_one({
                'user_id': self.user['_id'],
                'shopify_item_id': sold_product['product_id'],
            })

            if not shopify_item:
                update_order_product({
                    f'products.{key}.fulfillment_error': 'shopify_listings_missing',
                })

                logger.error("{product_id} | Not found in DB, probably not listed via JL", product_id=sold_product['product_id'])
                continue

            # Get related fba item
            fba_item = DB.FbaItems.find_one({
                '_id': shopify_item['fba_item_id'],
            })

            if not fba_item:
                update_order_product({
                    f'products.{key}.fulfillment_error': 'fba_listing_missing',
                })

                logger.error("{fba_item_id} | FBA item not found", fba_item_id=shopify_item['fba_item_id'])
                continue

            # Check if mfn_fulfilled before
            if sold_product.get('mfn_fulfilled'):
                logger.info("{product_id} | MFN fulfilled before, skipping", product_id=sold_product['product_id'])
                continue

            # If there is no amazon quantity but there is merchant quantity, allow MFN fulfillment
            if (not fba_item['amazon_quantity'] and fba_item.get('merchant_quantity', 0) > 0) or sold_product.get('amazon_product_type') == 'MFN':
                # Shopify accepts orders without shipping address (via API), so this is a failsafe
                try:
                    shipping_address = (
                        f"{order_from_db['customer']['address']['shipping']['name']}<br>"
                        f"{order_from_db['customer']['address']['shipping']['phone']}<br>"
                        f"{order_from_db['customer']['address']['shipping']['address1']} {order_from_db['customer']['address']['shipping']['address2']}<br>"
                        f"{order_from_db['customer']['address']['shipping']['zip']}<br>"
                        f"{order_from_db['customer']['address']['shipping']['province_code']} / {order_from_db['customer']['address']['shipping']['country_code']}"
                    )
                except Exception:
                    shipping_address = (
                        f"<a href='https://{self.shopify_creds['domain']}/admin/orders/{int(order_from_shopify['id'])}' target='_blank'>"
                        f"Please see details here"
                        f"</a>"
                    )

                Email().send(
                    to=self.user['emails'][0]['address'],
                    subject=settings.EMAILS.Fulfillment.MFN.Subject,
                    message=settings.EMAILS.Fulfillment.MFN.Body.format(
                        product_title=shopify_item['title'],
                        order_id=int(order_from_shopify['id']),
                        shipping_address=shipping_address,
                    ),
                )

                update_order_product({
                    f'products.{key}.mfn_fulfilled': True,
                })

                logger.info("{fba_item_id} | It's an MFN item, informing the user about manual fulfillment", fba_item_id=fba_item['_id'])
                continue

            # Gather sold products
            amazon_bought_items.append({
                'SellerSKU': str(fba_item['seller_sku']),
                'SellerFulfillmentOrderItemId': (str(fba_item['_id']) + ":JL:" + str(order_from_shopify['id']))[:50],
                'Quantity': str(sold_product['quantity']),
            })

        if not amazon_bought_items:
            DB.ShopifySales.update_one({'_id': order_from_db['_id']}, {
                '$set': {
                    'ignore_fulfilling': True,
                },
            })

            # Fulfill order on Shopify
            await self.make_shopify_fulfillment_request(order_from_db, shopify_shop_settings)

            return logger.info("All items in this order are MFNs.")

        # make fulfillment request to Amazon
        if not (request_id := self.make_amazon_fulfillment_request(order_from_db, order_from_shopify, amazon_bought_items)):
            return

        # Fulfill order on Shopify
        await self.make_shopify_fulfillment_request(order_from_db, shopify_shop_settings)

        # Email user and mark as shipped on Shopify
        if self.user['settings'].get('enable_email_notifications'):
            Email().send(
                to=self.user['emails'][0]['address'],
                subject=settings.EMAILS.Fulfillment.AFN.Subject,
                message=settings.EMAILS.Fulfillment.AFN.Body.format(
                    order_id=int(order_from_shopify['id']),
                    buyer_name=order_from_db['customer']['address'].get('shipping', {}).get('name', 'N/A'),
                ),
            )

    def make_amazon_fulfillment_request(self, order_from_db, order_from_shopify, amazon_bought_items):
        """ Inform Amazon about fulfillment """

        try:
            call = {
                # Load MWS Credentials
                'api': {
                    'accessKey': str(settings.AMAZON.Api.AccessKey),
                    'secretKey': str(settings.AMAZON.Api.SecretKey),
                    'sellerId': str(self.mws_creds['mws_merchant_id']),
                    'mwsToken': str(self.mws_creds.get('jl_mws_auth_token')),
                },
                # Sold items
                'items': amazon_bought_items,
                # Fill in the shipping address
                'address': {
                    'Name': str(order_from_db['customer']['address']['shipping']['name']),
                    'Line1': str(order_from_db['customer']['address']['shipping']['address1']),
                    'Line2': str(order_from_db['customer']['address']['shipping']['address2'] or ''),
                    'City': str(order_from_db['customer']['address']['shipping']['city']),
                    'StateOrProvinceCode': str(order_from_db['customer']['address']['shipping']['province_code'] or ''),
                    'CountryCode': 'US',
                    'PostalCode': str(order_from_db['customer']['address']['shipping']['zip']).upper(),
                    'PhoneNumber': str(order_from_db['customer']['address']['shipping']['phone'])[:20],
                },
                'params': {
                    'marketplace': settings.AMAZON.Api.MarketplaceUSA,
                    'orderId': str(order_from_shopify['id']),
                    'orderDate': datetime.utcnow().isoformat(),
                    'orderComment': 'Thanks for your order!',
                    'shippingSpeed': 'Standard',
                    'notificationEmail': str(order_from_db['contact_email']),
                }
            }
        except KeyError as e:
            DB.ShopifySales.update_one({'_id': order_from_db['_id']}, {
                '$set': {
                    'permanent_fulfillment_error': 'internal_error',
                },
            })

            logger.error(e)
            return

        amazon = mws.OutboundShipments(
            account_id=call['api']['sellerId'],
            access_key=call['api']['accessKey'],
            secret_key=call['api']['secretKey'],
            auth_token=call['api']['mwsToken'],
            region='US',
        )

        logger.debug("Making a fulfillment request {call}", call=call)

        try:
            response = amazon.create_fulfillment_order(
                marketplace_id=call['params']['marketplace'],
                seller_fulfillment_order_id=str(call['params']['orderId']),
                displayable_order_id=str(call['params']['orderId']),
                displayable_order_datetime=call['params']['orderDate'],
                displayable_order_comment=call['params']['orderComment'],
                shipping_speed_category=call['params']['shippingSpeed'],
                notification_email_list=call['params']['notificationEmail'],
                destination_address=call['address'],
                items=call['items'],
            )
        except Exception as e:
            error = xmltodict.parse(str(e))

            if "ErrorResponse" in str(e):
                amazon_error_message = error['ErrorResponse']['Error']['Message']

                if "No inventory available" in str(e):
                    DB.ShopifySales.update_one({'_id': order_from_db['_id']}, {
                        '$set': {
                            'permanent_fulfillment_error': 'no_inventory',
                        },
                    })

                    # Amazon returns sth like
                    # No inventory available for Items.SellerFulfillmentOrderItemId: 74cc2f18eac1500d1dd33941:JL:2791248363670
                    # Hence to get the erroneous product_id, this shit.
                    try:
                        from_text = 'No inventory available for Items.SellerFulfillmentOrderItemId: '
                        to_text = ':JL:'

                        problematic_fba_item_id = amazon_error_message[
                            amazon_error_message.find(from_text) + len(from_text) : amazon_error_message.rfind(to_text)
                        ]

                        problematic_fba_item = Helpers.get_fba_item(problematic_fba_item_id)
                    except Exception as e:
                        problematic_fba_item_id, problematic_fba_item = None, None
                        logger.error(e)

                    Email().send(
                        to=self.user['emails'][0]['address'],
                        subject=settings.EMAILS.Fulfillment.NoInventory.Subject,
                        message=settings.EMAILS.Fulfillment.NoInventory.Body.format(
                            order_id=int(order_from_shopify['id']),
                            product_title=problematic_fba_item['title'] if problematic_fba_item else 'N/A',
                            product_id=problematic_fba_item['_id'] if problematic_fba_item else 'N/A',
                        ),
                    )
                else:
                    DB.ShopifySales.update_one({'_id': order_from_db['_id']}, {
                        '$set': {
                            'permanent_fulfillment_error': amazon_error_message,
                        },
                    })

            logger.error(amazon_error_message)
            return

        try:
            # Parse out the RequestId
            return response.parsed.ResponseMetadata.RequestId
        except Exception as e:
            logger.error("Couldn't get request id, {e}", e=e)
            return

        # All good, store new fields
        DB.ShopifySales.update_one({'_id': order_from_db['_id']}, {
            '$set': {
                'amazon_fid': str(call['params']['orderId']),
                'fulfilled_at': datetime.utcnow(),
                'amazon_fba_request_id': request_id,
            },
            '$unset': {
                'permanent_fulfillment_error': True,
            },
        })

    async def make_shopify_fulfillment_request(self, order_from_db, shopify_shop_settings):
        """ Inform Shopify about fulfillment """

        async with aiohttp.ClientSession(headers=self.headers) as session:
            request = await session.post(
                url=settings.SHOPIFY.Endpoints.Fulfillments.format(
                    domain=self.shopify_creds['domain'],
                    order_id=int(order_from_db['order_id']),
                ),
                json={
                    'fulfillment': {
                        'location_id': shopify_shop_settings['primary_location_id'],
                        'notify_customer': True,
                    }
                }
            )

            # get response
            response = await request.json()

            # fulfillment_id is used by updating tracking afterwards
            DB.ShopifySales.update_one({'_id': order_from_db['_id']}, {
                '$set': {
                    'shopify_fulfillment_id': response['fulfillment']['id'] if request.status == 201 else None,
                }
            })

            logger.info(
                "Shopify fulfillment request resulted with: {result}",
                result='✓' if request.status == 201 else 'x',
            )
