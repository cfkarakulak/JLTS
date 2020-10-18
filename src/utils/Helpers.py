import aiohttp
import sys
import random
import inspect
import os

from loguru import logger
from utils.Database import Database
from decimal import Decimal

DB = Database.instance()


class Helpers:

    def configure_logger(user):

        try:
            filename = os.path.splitext(os.path.basename(inspect.stack()[1][0].f_code.co_filename))[0]
        except Exception:
            filename = 'undefined'

        # TODO: whitelist but not like that fucked up
        if filename not in ['ShopifyItemLister', 'ShopifyItemUnpublisher', 'ShopifyItemPublisher', 'ShopifyItemRepricer', 'ShopifyOrderFulfiller', 'ShopifyOrderTracker']:
            return

        return logger.configure(
            handlers=[
                {'sink': sys.stderr, 'format': filename + ' | <fg #fff>' + user + '</fg #fff> | {time:YYYY-MM-DD HH:mm:ss} | <level>{message}</level> <fg #444>@:{line}</fg #444>'},
                {'sink': f'logs/{filename}.log', 'rotation': '10 MB', 'format': filename + ' | <fg #fff>' + user + '</fg #fff> | {time:YYYY-MM-DD HH:mm:ss} | <level>{message}</level> <fg #444>@:{line}</fg #444>'},
            ],
            levels=[
                {"name": "DEBUG", "color": ""},
            ]
        )

    def generate_mongo_id():
        return('%024x' % random.randrange(16**24))

    def get_fba_item(fba_item_id):
        """ Fetch the listing by id """

        if not fba_item_id:
            return

        return DB.FbaItems.find_one({
            '_id': fba_item_id,
        })

    def get_user(user_id):
        """ Get related user """

        return DB.users.find_one({
            '_id': user_id,
        })

    def get_product_description(fba_item, subtract_ebay_footer=True):
        """Product description has different keys in different contexts"""

        product_desc = None

        if 'user_edited_details' in fba_item and fba_item['user_edited_details'].get('description'):
            product_desc = fba_item['user_edited_details'].get('description')
        elif 'scraped_product_details' in fba_item and fba_item['scraped_product_details'].get('cleaned_product_description'):
            product_desc = fba_item['scraped_product_details'].get('cleaned_product_description')
        elif 'scraped_product_details' in fba_item and fba_item['scraped_product_details'].get('product_description'):
            product_desc = fba_item['scraped_product_details'].get('product_description')
        elif 'description' in fba_item:
            product_desc = fba_item['description']

        if not product_desc:
            product_desc = Helpers.get_edited_or_default(fba_item, 'title')

        if subtract_ebay_footer:
            substring_char_start = product_desc.find('<div class="row store_desc softshadow">')

            if substring_char_start:
                product_desc = product_desc[:substring_char_start]

        return product_desc

    def get_amazon_price(fba_item):
        """Get Amazon price of the given fba_item"""

        amazon_price = None

        if amazon_price is None:
            amazon_price = (fba_item.get('pricing_info') or {}).get('amazon_price')

        if amazon_price is None:
            amazon_price = fba_item.get('sales_price')

        if amazon_price is None:
            amazon_price = fba_item.get('amazon_price')

        return amazon_price

    def get_edited_or_default(fba_item, key):
        """For selected FBA item, get the user edited or scraped data for given key"""

        # TODO: The 'null' only happens for ebay_category_id, fix that directly later
        if 'user_edited_details' in fba_item and fba_item['user_edited_details'].get(key) not in [None, 'null']:
            return fba_item['user_edited_details'][key]
        elif 'scraped_product_details' in fba_item and fba_item['scraped_product_details'].get(key) not in [None, 'null']:
            return fba_item['scraped_product_details'][key]
        elif key in fba_item:
            return fba_item[key]

    def get_product_quantity(fba_item):
        amazon_quantity = fba_item.get('amazon_quantity', 0)
        merchant_quantity = fba_item.get('merchant_quantity', 0)

        # if not quantity or amazon_quantity + merchant_quantity < quantity:
        quantity = amazon_quantity + merchant_quantity

        return quantity

    def format_price(price, precision=2):
        """ Format price with given precision """
        return str(round(Decimal(price), int(precision)))

    def get_price_by_formula(item_price, formula, price_type='sale_price'):
        """ Calculate price by given formula """

        item_price = float(item_price)

        if price_type == 'sale_price':
            # New price is going to be the fba item price by default
            sale_price = item_price
            pricing_option = formula.get('radioGroupShopifyPricing', 'equal')

            try:
                if pricing_option == 'percent':
                    sale_price = item_price * (1 + formula.get('shopifyPercentMargin', 0) / 100)
                elif pricing_option == 'fixed':
                    sale_price = item_price + formula.get('shopifyFixedMargin', 0)
            except ValueError:
                logger.warning("Margin value is not int")
                sale_price = item_price

            return 0 if sale_price < 0 else Helpers.format_price(sale_price, precision=2)

        if price_type == 'compare_at_price':
            # Compare at price is going to be the fba item price by default
            compare_at_price = item_price
            pricing_option = formula.get('radioGroupComparePricing', 'equal')

            try:
                if pricing_option == 'percent':
                    compare_at_price = item_price * (1 + formula.get('comparePercentMargin', 0) / 100)
                elif pricing_option == 'fixed':
                    compare_at_price = item_price + formula.get('compareFixedMargin', 0)
            except ValueError:
                logger.warning("Margin value is not int")
                compare_at_price = item_price

            return 0 if compare_at_price < 0 else Helpers.format_price(compare_at_price, precision=2)

    def get_shopify_options(settings, key, positive):
        """ Get user's Shopify options set on JoeLister """

        return True if settings.get('shopify_settings', {}).get(key, False) == positive else False

    async def fetch_shopify_settings(domain, headers):
        """ Get Shopify shop settings """

        SHOP_URL = (
            f"https://{domain}/admin/api/2019-10/"
            f"shop.json"
        )

        async with aiohttp.ClientSession(headers=headers) as session:
            try:
                response = await(await session.get(SHOP_URL)).json()

                if response.get('shop'):
                    return response['shop']
            except Exception:
                return False
