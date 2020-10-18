from loguru import logger
from requests import ReadTimeout, ConnectTimeout, HTTPError, Timeout, ConnectionError

class Retry:
    def log(details):
        logger.debug(
            "Backing off {wait:0.1f} seconds afters {tries} tries".format(**details)
        )

class TooManyRequestsException(Exception):
    """ Too many requests """

class ServerConnectionError(Exception):
    """ Connection somehow disrupted """
