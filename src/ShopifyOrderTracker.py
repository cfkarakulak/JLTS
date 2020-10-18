import asyncio
import aiohttp
import getopt
import bugsnag
import mws
import xmltodict

from loguru import logger
from datetime import datetime, timedelta
from dynaconf import settings
from utils.Database import Database
from utils.Queue import Queue
from utils.Helpers import Helpers
from utils.Email import Email

# Get DB instance
DB = Database.instance()


class ShopifyOrderTracker:

    def __init__(self):
        # Bugsnag for error reporting
        bugsnag.configure(api_key=settings.APP.Bugsnag.Key)

    async def process(self, payload):
        """ Gets called every time when a message is consumed """

        # Reject if not my job
        if payload['task'] != 'Track':
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
        if Helpers.get_shopify_options(self.user['settings'], 'fullFillment', 'off'):
            return logger.debug("{user_id} | Fulfillment service disabled by the user", user_id=payload['user_id'])

        # Configure logger format
        Helpers.configure_logger(user=self.user['_id'])

        # Fetch latest sales that are fulfilled
        await self.get_transactions()

    async def get_transactions(self):
        """ Get Shopify transactions of current user """

        # Get Shopify sale count for the user
        shopify_orders_count = DB.ShopifySales.count_documents({
            'user_id': self.user['_id'],
            'tracking_info': {'$exists': False},
            'amazon_fid': {'$exists': True},
            'zma_request_id': {'$exists': False},
            # 'fulfilled_at': {'$gt': datetime.utcnow() - timedelta(days=7)},
        })

        if not shopify_orders_count:
            return logger.debug("No untracked transaction found")

        # Get shopify sale in our DB
        shopify_orders = DB.ShopifySales.find({
            'user_id': self.user['_id'],
            'tracking_info': {'$exists': False},
            'amazon_fid': {'$exists': True},
            'zma_request_id': {'$exists': False},
            # 'fulfilled_at': {'$gt': datetime.utcnow() - timedelta(days=7)},
        })

        for order in shopify_orders:
            try:
                call = {
                    # Load MWS Credentials
                    'api': {
                        'accessKey': str(settings.AMAZON.Api.AccessKey),
                        'secretKey': str(settings.AMAZON.Api.SecretKey),
                        'sellerId': str(self.mws_creds['mws_merchant_id']),
                        'mwsToken': str(self.mws_creds.get('jl_mws_auth_token')),
                    },
                    'params': {
                        'SellerFulfillmentOrderId': order['amazon_fid'],
                    },
                }
            except KeyError as e:
                logger.error(e)
                continue

            amazon = mws.OutboundShipments(
                account_id=call['api']['sellerId'],
                access_key=call['api']['accessKey'],
                secret_key=call['api']['secretKey'],
                auth_token=call['api']['mwsToken'],
                region='US',
            )

            logger.debug("Making a get fulfillment request {call}", call=call)

            try:
                response = amazon.get_fulfillment_order(
                    seller_fulfillment_order_id=call['params']['SellerFulfillmentOrderId'],
                )
            except Exception as e:
                error = xmltodict.parse(str(e))

                logger.error(error['ErrorResponse']['Error']['Message'])
                continue

            try:
                # Get the package details
                shipment_package = response.parsed.FulfillmentShipment.member.FulfillmentShipmentPackage.member

                # Update the current order with the tracking information
                DB.ShopifySales.update_one({'_id': order['_id']}, {
                    '$set': {
                        'tracking_info': {
                            'package_number': shipment_package.PackageNumber,
                            'tracking_number': shipment_package.TrackingNumber,
                            'carrier_code': shipment_package.CarrierCode,
                            'estimated_arrival': shipment_package.EstimatedArrivalDateTime,
                        },
                    },
                })

                if order.get('shopify_fulfillment_id'):
                    await self.make_shopify_fulfillment_request(order, shipment_package)

                Email().send(
                    to=self.user['emails'][0]['address'],
                    subject=settings.EMAILS.Tracking.Obtained.Subject,
                    message=settings.EMAILS.Tracking.Obtained.Body.format(
                        order_id=int(order['order_id']),
                        carrier_code=shipment_package.CarrierCode,
                        tracking_number=shipment_package.TrackingNumber,
                        estimated_arrival=shipment_package.EstimatedArrivalDateTime,
                    ),
                )

                logger.success("{order_id} | Updated order with the tracking info and notified the customer", order_id=order['_id'])
            except Exception as e:
                logger.debug("{fid} | Tracking is not yet ready", fid=call['params']['SellerFulfillmentOrderId'])
                continue

    async def make_shopify_fulfillment_request(self, order, shipment_package):
        """ Inform Shopify about fulfillment """

        # Set header
        self.headers = {
            'Content-Type': 'application/json',
            'X-Shopify-Access-Token': self.shopify_creds['token'],
        }

        async with aiohttp.ClientSession(headers=self.headers) as session:
            request = await session.put(
                url=settings.SHOPIFY.Endpoints.Fulfillment.format(
                    domain=self.shopify_creds['domain'],
                    order_id=int(order['order_id']),
                    fulfillment_id=int(order['shopify_fulfillment_id']),
                ),
                json={
                    'fulfillment': {
                        'tracking_number': shipment_package.TrackingNumber,
                        "tracking_url": f"https://packagetrackr.com/track/{shipment_package.TrackingNumber}",
                        'notify_customer': True,
                    }
                }
            )

            logger.info(
                "Shopify tracking request resulted with: {result}",
                result='✓' if request.status == 200 else 'x',
            )
