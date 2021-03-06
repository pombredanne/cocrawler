'''
Code related to generating webpage facets.

Two storage sizes:
   big -- all headers
   small -- summary only
Two speeds:
   fast - avoid expensive greps
   slow - grep everything

For normal crawling, we only parse facets we think might be useful
for crawling and ranking: STS, twitter cards, facebook opengraph.

TODO: find rss feeds (both link alternate and plain href to .xml or maybe .rss)
TODO: probe with DNT:1 and see who replies TK: N

This module also contains code to post-facto process headers to
figure out what technologies are used in a website.

'''

import re
import logging

from bs4 import BeautifulSoup

from . import stats

LOGGER = logging.getLogger(__name__)

save_x_headers = set(('x-powered-by', 'cf-ray', 'x-generator'))

special_image = set(('og:image', 'twitter:image'))


meta_name_content = set(('twitter:site', 'twitter:site:id', 'twitter:creator', 'twitter:creator:id',
                         'robots', 'charset', 'http-equiv', 'referrer', 'format-detection', 'generator',
                         'parsely-title', 'apple-itunes-app', 'google-play-app'))
meta_name_generator_special = ('wordpress', 'movable type', 'drupal')
meta_name_prefix = (('twitter:', 'twitter card'),)

meta_property_content = set(('twitter:site', 'twitter:site:id', 'twitter:creator', 'twitter:creator:id',
                             'fb:app_id', 'fb:admins'))
meta_property_prefix = (('al:', 'applinks'),
                        ('og:', 'opengraph'),
                        ('article:', 'opengraph'),
                        ('op:', 'fb instant'),
                        ('bt:', 'boomtrain'),)

link_rel = set(('canonical', 'alternate', 'amphtml', 'opengraph', 'origin'))

save_response_headers = ('refresh', 'server', 'set-cookie', 'strict-transport-security', 'tk')


def compute_all(html, head, body, headers_list, links, embeds, head_soup=None, url=None, condense=False, expensive=False):
    expensive = True  # XXX

    fhf = find_head_facets(head, head_soup=head_soup, url=url)
    fgh = facets_grep(head, url=url)
    if expensive:
        fgb = facets_grep(body, url=url)
        compare_head_body_grep(fgh, fgb, url)
    else:
        fgb = []
    frh = facets_from_response_headers(headers_list)
    fe = facets_from_embeds(embeds)

    facets = [*fhf, *fgh, *fgb, *frh, *fe]

    for l in links:
        facets.append(('link', l.url))
    for e in embeds:
        facets.append(('embed', e.url))

    return facet_dedup(facets)


def find_head_facets(head, head_soup=None, url=None):
    facets = []

    if head_soup is None:
        stats.stats_sum('beautiful soup head bytes', len(head))
        with stats.record_burn('beautiful soup head', url=url):
            try:
                head_soup = BeautifulSoup(head, 'lxml')
            except Exception as e:
                facets.append(('BeautifulSoupException', repr(e)))
                return facets

    html = head_soup.find('html')
    if html:
        if html.get('lang'):
            facets.append(('html lang', html.get('lang')))
        if html.get('xml:lang'):
            facets.append(('html xml:lang', html.get('xml:lang')))

    base = head_soup.find('base')
    if base:
        if base.get('href'):
            facets.append(('base', base.get('href')))
            # can also have target= but we don't care

    meta = head_soup.find_all('meta', attrs={'name': True})  # 'name' collides, so use dict
    for m in meta:
        n = m.get('name').lower()
        content = m.get('content')
        #if n in meta_name_content:
        #    facets.append((n, content)
        if n is not '':
            if len(content) > 100:
                content = content[:100]
            facets.append(('meta-name-'+n, content))  # XXX get all of these for now
        if n == 'generator' and content is not None:
            cl = content.lower()
            for s in meta_name_generator_special:
                if s in cl:
                    facets.append((s, True))
        for pre in meta_name_prefix:
            prefix, title = pre
            if n.startswith(prefix):
                facets.append((title, True))
        # XXX remember the ones we didn't save

    meta = head_soup.find_all('meta', property=True)
    for m in meta:
        p = m.get('property').lower()
        content = m.get('content')
        if len(content) > 100:
            content = content[:100]
        if p is not '':
            facets.append(('meta-property-'+p, content))  # XXX get all of these for now
        if p in meta_property_content:
            facets.append((p, content))
        for pre in meta_property_prefix:
            prefix, title = pre
            if p.startswith(prefix):
                facets.append((title, True))
        # XXX remember the ones we didn't save

    meta = head_soup.find_all('meta', attrs={'http-equiv': True})  # has a dash, so use dict
    for m in meta:
        p = m.get('http-equiv').lower()
        content = m.get('content')
        if len(content) > 100:
            content = content[:100]
        facets.append((p, content))  # XXX get all of these for now... robots, refresh etc

    # link rel is muli-valued attribute, hence, a list
    linkrel = head_soup.find_all('link', rel=True)
    for l in linkrel:
        for rel in l.get('rel'):
            r = rel.lower()
            if r in link_rel:
                # type is useful if it's something like canonical + type=rss
                facets.append(('link-rel-'+r, (l.get('href', 'nohref'), l.get('type', 'notype'))))
            else:
                # XXX remember the ones we didn't save
                pass
            href = l.get('href')
            if href is not None:
                if (('http://microformats.org/' in href or
                     'https://microformats.org/' in href)):
                    facets.append(('microformats.org', True))

    count = len(head_soup.find_all(integrity=True))
    if count:
        facets.append(('script integrity', count))

    return facets


def facet_dedup(facets):
    '''
    Remove duplicate ('foo', True) facets. Keep all the ones with other values.
    '''
    if not facets:
        return []

    dups = set()
    ret = []
    for f in facets:
        a, b = f
        if b is True:
            if a not in dups:
                ret.append((a, b))
                dups.add(a)
        else:
            ret.append((a, b))
    return ret


def facets_grep(html, url=None):
    facets = []

    # if present, it's embedded in a <script> jsonl in the head or body
    if 'http://schema.org' in html or 'https://schema.org' in html:
        facets.append(('schema.org', True))

    # this can be in js or a cgi arg
    if 'pub-' in html:
        pub_matches = re.findall(r'[\'"\-=]pub-\d{16}[\'"&]', html)
        if pub_matches:
            for p in pub_matches:
                facets.append(('google publisher id', p.strip('\'"-=&')))
        else:
            LOGGER.info('url %s had false positive for pub- facet', url.url)

    # this can be in js or a cgi arg
    if 'UA-' in html:
        ga_matches = re.findall(r'[\'"\-=]UA-\d{6,9}-\d{1,3}[\'"&]', html)
        if ga_matches:
            for g in ga_matches:
                facets.append(('google analytics', g.strip('\'"-=&')))
        else:
            # frequent false positive for meta http-equiv X-UA-Compatible, alas
            pass

    # js or id= cgi arg
    if 'GTM-' in html:
        gtm_matches = re.findall(r'[\'"\-=]GTM-[A-Z0-9]{4,7}[\'"&]', html)
        if gtm_matches:
            for g in gtm_matches:
                facets.append(('google tag manager', g.strip('\'"-=&')))
        else:
            LOGGER.info('url %s had false positive for GTM- facet', url.url)

    # script: fbq('init', '\d{16}', and https://connect.facebook.net/en_US/fbevents.js
    # this could be skipped if we analyze embeds first -- standard FB code has both
    if 'fbq(' in html:
        fbid_matches = re.findall(r'fbq\( \s? [\'"] init [\'"] , \s? [\'"] (\d{16}) [\'"]', html, re.X)
        if fbid_matches:
            for g in fbid_matches:
                facets.append(('facebook events', g))
        else:
            LOGGER.info('url %s had false positive for facebook events facet', url.url)

    return facets


def facets_from_response_headers(headers_list):
    '''
    Extract facets from headers. All are useful for site software fingerprinting but
    for now we'll default to grabbing the most search-enginey ones
    '''
    facets = []
    for h in headers_list:
        k, v = h
        #if k in save_response_headers:
        #    facets.append(('header-'+k, v))
        facets.append(('header-'+k, v))  # XXX save them all for one run

    return facets


# XXX should be generalized using lists from adblockers
def facets_from_embeds(embeds):
    facets = []
    for url in embeds:  # this is both href and src embeds, but whatever
        u = url.url
        if 'cdn.ampproject.org' in u:
            facets.append(('google amp', True))
        if 'www.google-analytics.com' in u:
            # rare that it's used this way
            # XXX parse the publisher id out of the cgi
            facets.append(('google analytics link', True))
        if 'googlesyndication.com' in u:
            facets.append(('google adsense', True))
        if 'google.com/adsense/domains' in u:
            facets.append(('google adsense for domains', True))
        if 'googletagmanager.com' in u:
            cgi = url.urlsplit.query
            cgi_list = cgi.split('&')
            for c in cgi_list:
                if c.startswith('id=GTM-'):
                    facets.append(('google tag manager', c[3:]))
        if 'https://www.facebook.com/tr?' in u:  # img src
            cgi = url.urlsplit.query
            cgi_list = cgi.split('&')
            for c in cgi_list:
                if c.startswith('id='):
                    facets.append(('facebook events', c[3:]))

    return facets


def compare_head_body_grep(fh, fb, url):
    '''
    We only occasionally run body greps, and there are unique ids
    that only appear in the body.
    '''
    head = set(fh)
    body = set(fb)
    for kv in body:
        k, v = kv
        if kv not in head:
            LOGGER.info('body grep discovered %s %s in url %s', k, v, url.url)
        else:
            LOGGER.info('both head and body grep discovered %s %s in url %s', k, v, url.url)


def condense_facets(facets):
    # turn foo:bar into foo:<count>
    #   meta name, meta property
    # ditch traditionally long things: meta-name-{description,keywords}
    count_colons('meta-property-', facets)
    count_colons('meta-name-', facets)

    return
