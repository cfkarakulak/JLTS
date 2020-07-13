import os
import logging

from domain.Shopify.utils.Database import Database

logging.basicConfig()
LOG = logging.getLogger()
DB = Database.instance()


class Helpers:
    """ Queue helper for most common methods """
    def get_edited_or_default(fba_item, key, logid):
        """For selected FBA item, get the user edited or scraped data for given key"""

        # TODO: The 'null' only happens for ebay_category_id, fix that directly later
        if 'user_edited_details' in fba_item and fba_item['user_edited_details'].get(key) not in [None, 'null']:
            return fba_item['user_edited_details'][key]
        elif 'scraped_product_details' in fba_item and fba_item['scraped_product_details'].get(key) not in [None, 'null']:
            return fba_item['scraped_product_details'][key]
        elif key in fba_item:
            return fba_item[key]
        else:
            LOG.info("%s: Failed to find %s in fba_item", logid, key)

    def get_product_description(fba_item, logid):
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
            LOG.info("%s: Failed to find value for product description in fba_item", logid)
            product_desc = Helpers.get_edited_or_default(fba_item, 'title', logid)

        return product_desc
