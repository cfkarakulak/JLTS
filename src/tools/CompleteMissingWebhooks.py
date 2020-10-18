import asyncio
import aiohttp
import os
import sys

currentdir = os.path.dirname(os.path.realpath(__file__))
parentdir = os.path.dirname(currentdir)
sys.path.append(parentdir)

from loguru import logger
from utils.Database import Database

# Get DB instance
DB = Database.instance()

SHOPIFY_WEBHOOK_URL = 'https://app.joelister.com/shopify_event'
SHOPIFY_WEBHOOK_TOPICS = ['orders/create', 'products/delete', 'products/update']

async def main():
    # Get users with Shopify token
    users = DB.users.find({
        'settings.shopify_creds.token': {'$exists': True},
    })

    for user in users:
        URL = (
            f"https://{user['settings']['shopify_creds']['domain']}/admin/api/2019-10/"
            f"webhooks.json"
        )

        # Set header
        headers = {
            'Content-Type': 'application/json',
            'X-Shopify-Access-Token': user['settings']['shopify_creds']['token'],
        }

        async with aiohttp.ClientSession(headers=headers) as session:
            request = await session.get(URL)
            response = await request.json()

        # topics of existing hooks with correct URLs
        existingHooks = map(lambda x: x['topic'], filter(lambda x: x['address'] == SHOPIFY_WEBHOOK_URL, response.get('webhooks', {})))

        # topics of hooks that we want to add
        neededHooks = filter(lambda x: x not in existingHooks, SHOPIFY_WEBHOOK_TOPICS)

        if not neededHooks:
            return logger.info("{user} | has no missing webhooks", user=user['_id'])

        for topic in neededHooks:
            logger.info("{user} | creating {topic} topic", user=user['_id'], topic=topic)

            data = {
                "webhook": {
                    "topic": topic,
                    "address": SHOPIFY_WEBHOOK_URL,
                    "format": "json",
                }
            }

            async with aiohttp.ClientSession(headers=headers) as session:
                request = await session.post(URL, json=data)
                response = await request.json()

                if request.status == 201:
                    logger.info("{user} | {topic} topic created", user=user['_id'], topic=topic)

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
