'''
Stuff related to robots.txt processing
'''

import asyncio

import time
import random
import json
import logging
import urllib.parse
import hashlib

import robotexclusionrulesparser
import magic

from .urls import URL
from . import stats
from . import fetcher
from . import config

LOGGER = logging.getLogger(__name__)


def strip_bom(b):
    if b[:3] == b'\xef\xbb\xbf':  # utf-8, e.g. microsoft.com's sitemaps
        return b[3:].strip()
    elif b[:2] in (b'\xfe\xff', b'\xff\xfe'):  # utf-16 BE and LE, respectively
        return b[2:].strip()
    else:
        return b.strip()


def preprocess_robots(text):
    '''
    robotsexclusionrulesparser does not follow the de-factor robots.txt standard.
    1) blank lines should not reset user-agent to *
    2) longest match
    This code preprocesses robots.txt to mitigate (1)
    TODO: make wrap robotsexclusionrulesparser in another class?

    Note: Python's built-in urllib.robotparser definitely breaks (1)
    '''
    ret = ''
    # convert line endings
    text = text.replace('\r', '\n')
    for line in text.split('\n'):
        line = line.lstrip()
        if len(line) > 0 and not line.startswith('#'):
            ret += line + '\n'
    return ret


class Robots:
    def __init__(self, robotname, session, datalayer):
        self.robotname = robotname
        self.session = session
        self.datalayer = datalayer
        self.max_tries = config.read('Robots', 'MaxTries')
        self.max_robots_page_size = int(config.read('Robots', 'MaxRobotsPageSize'))
        self.in_progress = set()
        self.magic = magic.Magic(flags=magic.MAGIC_MIME_TYPE)
        self.robotslog = config.read('Logging', 'Robotslog')
        if self.robotslog:
            self.robotslogfd = open(self.robotslog, 'a')
        else:
            self.robotslogfd = None

    def __del__(self):
        if self.magic is not None:
            self.magic.close()
        if self.robotslogfd:
            self.robotslogfd.close()

    async def check(self, url, headers=None, proxy=None, mock_robots=None):
        schemenetloc = url.urlsplit.scheme + '://' + url.urlsplit.netloc

        try:
            robots = self.datalayer.read_robots_cache(schemenetloc)
            stats.stats_sum('robots cache hit', 1)
        except KeyError:
            robots = await self.fetch_robots(schemenetloc, mock_robots,
                                             headers=headers, proxy=proxy)

        # XXX I don't know why I'm building this up, shouldn't I use url.url?
        if url.urlsplit.path:
            pathplus = url.urlsplit.path
        else:
            pathplus = '/'
        if url.urlsplit.query:
            pathplus += '?' + url.urlsplit.query

        if robots is None:
            LOGGER.debug('no robots info known for %s, failing %s%s', schemenetloc, schemenetloc, pathplus)
            self.jsonlog(schemenetloc, {'error': 'no robots info known', 'action': 'deny'})
            stats.stats_sum('robots denied - robots info not known', 1)
            stats.stats_sum('robots denied', 1)
            return False

        with stats.record_burn('robots is_allowed', url=schemenetloc):
            check = robots.is_allowed(self.robotname, pathplus)
            if not check:
                google_check = robots.is_allowed('googlebot', pathplus)
                generic_check = robots.is_allowed('*', pathplus)

        if check:
            LOGGER.debug('robots allowed for %s%s', schemenetloc, pathplus)
            stats.stats_sum('robots allowed', 1)
            return True

        LOGGER.debug('robots denied for %s%s', schemenetloc, pathplus)

        json_log = {'url': pathplus, 'action': 'deny'}
        if google_check:
            json_log['google-action'] = 'allow'
        if generic_check:
            json_log['generic-action'] = 'allow'
        self.jsonlog(schemenetloc, json_log)
        stats.stats_sum('robots denied', 1)
        if google_check:
            stats.stats_sum('robots denied - but googlebot allowed', 1)
        if generic_check:
            stats.stats_sum('robots denied - but * allowed', 1)
        return False

    def _cache_empty_robots(self, schemenetloc, final_schemenetloc):
        parsed = robotexclusionrulesparser.RobotExclusionRulesParser()
        parsed.parse('')
        self.datalayer.cache_robots(schemenetloc, parsed)
        if final_schemenetloc:
            self.datalayer.cache_robots(final_schemenetloc, parsed)
        self.in_progress.discard(schemenetloc)
        return parsed

    async def fetch_robots(self, schemenetloc, mock_url, headers=None, proxy=None):
        '''
        robotexclusionrules fetcher is not async, so fetch the file ourselves

        https://developers.google.com/search/reference/robots_txt
        3xx redir == follow up to 5 hops, then consider it a 404.
        4xx errors == no crawl restrictions
        5xx errors == full disallow. fast retry if 503.
           if site appears to return 5xx for 404, then 5xx is treated as a 404
        '''
        url = URL(schemenetloc + '/robots.txt')

        if proxy:
            raise ValueError('not yet implemented')

        # We might enter this routine multiple times, so, sleep if we aren't the first
        # XXX this is frequently racy, according to the logfiles!
        if schemenetloc in self.in_progress:
            while schemenetloc in self.in_progress:
                LOGGER.debug('sleeping because someone beat me to the robots punch')
                # XXX make this a stat?
                with stats.coroutine_state('robots collision sleep'):
                    interval = random.uniform(0.2, 0.3)
                    await asyncio.sleep(interval)

            # at this point robots might be in the cache... or not.
            try:
                robots = self.datalayer.read_robots_cache(schemenetloc)
            except KeyError:
                robots = None
            if robots is not None:
                return robots

            # ok, so it's not in the cache -- and the other guy's
            # fetch failed. if we just fell through there would be a
            # big race. treat this as a failure.
            # XXX note that we have no negative caching
            LOGGER.debug('some other fetch of robots has failed.')  # XXX make this a stat
            return None

        self.in_progress.add(schemenetloc)

        f = await fetcher.fetch(url, self.session, max_page_size=self.max_robots_page_size,
                                headers=headers, proxy=proxy, mock_url=mock_url,
                                allow_redirects=True, max_redirects=5, stats_prefix='robots ')

        json_log = {'action': 'fetch'}
        if f.last_exception:
            json_log['error'] = 'max tries exceeded, final exception is: ' + f.last_exception
            self.jsonlog(schemenetloc, json_log)
            self.in_progress.discard(schemenetloc)
            return None

        stats.stats_sum('robots fetched', 1)

        # If the url was redirected to a different host/robots.txt, let's cache that too
        # XXX use f.response.history to get them all
        final_url = str(f.response.url)  # this is a yarl.URL object now -- str() or url.human_repr()? XXX
        final_schemenetloc = None
        if final_url != url.url:
            final_parts = urllib.parse.urlsplit(final_url)
            if final_parts.path == '/robots.txt':
                final_schemenetloc = final_parts.scheme + '://' + final_parts.netloc

        status = f.response.status
        json_log['status'] = status
        json_log['t_first_byte'] = f.t_first_byte

        # if the final status is a redirect, we exceeded max redirects -- treat as a 404, same as googlebot
        # Googlebot treats all 4xx as an empty robots.txt
        if str(status).startswith('3') or str(status).startswith('4'):
            if status >= 400:
                error = 'got a 4xx, treating as empty robots'
            else:
                error = 'got too many redirects, treating as empty robots'
            json_log['error'] = error
            self.jsonlog(schemenetloc, json_log)
            return self._cache_empty_robots(schemenetloc, final_schemenetloc)

        # Googlebot treats all 5xx as deny, unless they think the host returns 5xx instead of 404:
        if str(status).startswith('5'):
            json_log['error'] = 'got a 5xx, treating as deny'
            self.jsonlog(schemenetloc, json_log)
            self.in_progress.discard(schemenetloc)
            return None

        body_bytes = f.body_bytes

        with stats.record_burn('robots sha1'):
            sha1 = 'sha1:' + hashlib.sha1(body_bytes).hexdigest()
        json_log['checksum'] = sha1

        body_bytes = strip_bom(body_bytes)

        plausible, message = self.is_plausible_robots(schemenetloc, f.body_bytes, f.t_first_byte)
        if not plausible:
            # policy: treat as empty
            json_log['error'] = 'saw an implausible robots.txt, treating as empty'
            json_log['implausible'] = message
            self.jsonlog(schemenetloc, json_log)
            return self._cache_empty_robots(schemenetloc, final_schemenetloc)

        # go from bytes to a string, despite bogus utf8
        # XXX what about non-utf8?
        try:
            body = f.body_bytes.decode(encoding='utf8')
        except UnicodeError:  # pragma: no cover
            # try again assuming utf8 and ignoring errors
            body = f.body_bytes.decode(encoding='utf8', errors='replace')
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # log as surprising, also treat like a fetch error
            json_log['error'] = 'robots body decode threw a surprising exception: ' + repr(e)
            self.jsonlog(schemenetloc, json_log)
            self.in_progress.discard(schemenetloc)
            return None

        if self.robotname in body:
            json_log['mentions-us'] = True

        with stats.record_burn('robots parse', url=schemenetloc):
            parsed = robotexclusionrulesparser.RobotExclusionRulesParser()
            parsed.parse(preprocess_robots(body))
        self.datalayer.cache_robots(schemenetloc, parsed)
        self.in_progress.discard(schemenetloc)
        if final_schemenetloc:
            # we did not set this but we'll discard it anyway
            self.in_progress.discard(final_schemenetloc)
        if parsed.sitemaps:
            json_log['has-sitemaps'] = True

        self.jsonlog(schemenetloc, json_log)
        return parsed

    def is_plausible_robots(self, schemenetloc, body_bytes, t_first_byte):
        '''
        Did you know that some sites have a robots.txt that's a 100 megabyte video file?
        '''
        # Not OK: html or xml or something else bad
        if body_bytes.startswith(b'<'):  # pragma: no cover
            return False, 'robots appears to be html or xml'

        # file magic mimetype is 'text' or similar -- too expensive, 3ms per call
        # mime_type = self.magic.id_buffer(body_bytes)
        # if not (mime_type.startswith('text') or mime_type == 'application/x-empty'):
        #    return False, 'robots has unexpected mimetype {}, ignoring'.format(mime_type)

        # not OK: too big
        if len(body_bytes) > 1000000:  # pragma: no cover
            return False, 'robots is too big'

        return True, ''

    def jsonlog(self, schemenetloc, d):
        if self.robotslogfd:
            json_log = d
            json_log['host'] = schemenetloc
            json_log['time'] = time.time()
            print(json.dumps(json_log, sort_keys=True), file=self.robotslogfd)
