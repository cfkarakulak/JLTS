from dynaconf import settings


class API:
    """ Shopify API wrapper class """

    def __init__(self, **kwargs):
        self.url = f"https://{settings.DOMAIN.Shopify.API.Store}/admin/api/{settings.DOMAIN.Shopify.API.Version}/{kwargs['endpoint']}"
        self.headers = {**{
            "X-Shopify-Access-Token": settings.DOMAIN.Shopify.API.Token,
        }, **kwargs.get('headers', {})}
