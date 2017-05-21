'''
Wrappers for WARC stuff

TODO

best-practice:
WARC-Warcinfo-ID for every record
computation:
stick pre-computed digest into WARC-Payload-Digest

XXX BUG we are not getting the full request headers because the request object is immediately destroyed by aiohttp

'''

import os
import socket
import logging
from collections import OrderedDict
from io import BytesIO

import six

try:
    import collections.abc as collections_abc  # only works on python 3.3+
except ImportError:
    import collections as collections_abc

from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter
from warcio.timeutils import timestamp_now

from . import stats

LOGGER = logging.getLogger(__name__)


'''
best practices from http://www.netpreserve.org/sites/default/files/resources/WARC_Guidelines_v1.pdf

filenames: prefix-timestamp-serial-crawlhost.warc.gz
 encourage users to configure prefix different for each crawl
  example: prefix=BNF-CRAWL-003
 serial should be unique wrt prefix

warcinfo at the start of every file: WARC-Filename in case of a rename, repeat crawl configuration info
have a WARC-Warcinfo-ID field for every record
logfiles in a warcinfo record (might have to segment if big)
final warc file: record with a manifest of all warcs created in the crawl
'''

'''
warcinfo content from heretrix:

software: Heritrix 1.12.0 http://crawler.archive.org
hostname: crawling017.archive.org
ip: 207.241.227.234
isPartOf: testcrawl-20050708 {{Dublin Core}}
description: testcrawl with WARC output {{Dublin Core}}}
operator: IA\_Admin {XXX why the \_?} {{1.0 standard says should be contact info, name or name and email}}
http-header-user-agent:
 Mozilla/5.0 (compatible; heritrix/1.4.0 +http://crawler.archive.org) {{redundant with info in request record}}
format: WARC file version 1.0 {{Dublin Core}}
conformsTo:
 http://www.archive.org/documents/WarcFileFormat-1.0.html {{Dublin Core}}

warcinfo from warcio package example.warc:

WARC/1.0
WARC-Date: 2017-03-06T04:03:53Z
WARC-Record-ID: <urn:uuid:e9a0ee48-0221-11e7-adb1-0242ac120008>
WARC-Filename: temp-20170306040353.warc.gz
WARC-Type: warcinfo
Content-Type: application/warc-fields
Content-Length: 470

software: Webrecorder Platform v3.7
format: WARC File Format 1.0
creator: temp-MJFXHZ4S {{Dublin Core: person, organization, or service}}
isPartOf: Temporary%20Collection/Recording%20Session
json-metadata: {"created_at": 1488772924, "type": "recording", "updated_at": 1488773028, "title": "Recording Session", "size": 2865, "pages": [{"url": "http://example.com/", "title": "Example Domain", "timestamp": "20170306040348"}, {"url": "http://example.com/", "title": "Example Domain", "timestamp": "20170306040206"}]}
'''


class CCWARCWriter:
    def __init__(self, prefix, max_size, subprefix=None, gzip=True, get_serial=None):
        self.writer = None
        self.prefix = prefix
        self.subprefix = subprefix
        self.max_size = max_size
        self.gzip = gzip
        self.hostname = socket.gethostname()
        if get_serial is not None:
            self.external_get_serial = get_serial
        else:
            self.external_get_serial = None
            self.serial = 0

    def __del__(self):
        if self.writer is not None:
            self.f.close()

    def create_default_info(self, version, ip, description=None, creator=None, operator=None):
        '''
        creator:  # person, organization, service
        operator:  # person, if creator is an organization
        isPartOf:  # name of the crawl
        '''
        info = OrderedDict()

        info['software'] = 'cocrawler/' + version
        info['hostname'] = self.hostname
        info['ip'] = ip
        if description:
            info['description'] = description
        if creator:
            info['creator'] = creator
        if operator:
            info['operator'] = operator
        info['isPartOf'] = self.prefix  # intentionally does not include subprefix
        info['format'] = 'WARC file version 1.0'
        self.info = info
        return info

    def open(self):
        filename = self.prefix
        if self.subprefix:
            filename += '-' + self.subprefix
        serial = self.get_serial(filename)
        filename += '-' + serial + '-' + self.hostname + '.warc'
        if self.gzip:
            filename += '.gz'
        self.filename = filename
        self.f = open(filename, 'wb')
        self.writer = WARCWriter(self.f, gzip=self.gzip)
        record = self.writer.create_warcinfo_record(self.filename, self.info)
        self.writer.write_record(record)

    def get_serial(self, filename):
        if self.external_get_serial is not None:
            return self.external_get_serial(filename)
        self.serial += 1
        return '{:06}'.format(self.serial-1)

    def maybe_close(self):
        '''
        TODO: always close/reopen if subprefix is not None; minimizes open filehandles
        '''
        fsize = os.fstat(self.f.fileno()).st_size
        if fsize > self.max_size:
            self.f.close()
            self.writer = None

    def write_dns(self, host, kind, result):
        #  response record, content-type 'text/dns', contents as defined by rfcs 2540 and 1035
        #  uri = dns:www.example.com
        # example payload:
        #  20130522085319
        #  fue-l.onb1.ac.at.3600	IN	A	172.16.14.151
        # dns responses can surprise you -- pycares doesn't do this right now but might in the future
        # ;; ANSWER SECTION:
        # blog.greglindahl.com.	299	IN	CNAME	ghs.google.com.
        # ghs.google.com.	86399	IN	CNAME	ghs.l.google.com.
        # ghs.l.google.com.	299	IN	A	172.217.5.115

        # write it out even if empty

        if self.writer is None:
            self.open()

        payload = timestamp_now() + '\r\n'

        for r in result:
            try:
                payload += host + '.\t' + str(r.ttl) + '\tIN\t' + kind + '\t' + r.host + '\r\n'
            except Exception as e:
                LOGGER.info('problem converting dns reply for warcing', r, e)
                pass
        payload = payload.encode('utf-8')

        record = self.writer.create_warc_record('dns:'+host, 'resource', payload=BytesIO(payload),
                                                warc_content_type='text/dns', length=len(payload))

        self.writer.write_record(record)
        LOGGER.debug('wrote warc dns response record'+p(self.prefix), 'for host', host)
        stats.stats_sum('warc dns'+p(self.prefix), 1)

    def write_request_response_pair(self, url, req_headers, resp_headers, payload, digest=None):
        if self.writer is None:
            self.open()

        req_http_headers = StatusAndHeaders('GET / HTTP/1.1', headers_to_str_headers(req_headers))

        request = self.writer.create_warc_record('http://example.com/', 'request',
                                                 http_headers=req_http_headers)

        resp_http_headers = StatusAndHeaders('200 OK', headers_to_str_headers(resp_headers), protocol='HTTP/1.1')

        warc_headers_dict = {}
        if digest is not None:
            warc_headers_dict['WARC-Payload-Digest'] = digest

        response = self.writer.create_warc_record(url, 'response',
                                                  payload=BytesIO(payload),
                                                  length=len(payload),
                                                  warc_headers_dict=warc_headers_dict,
                                                  http_headers=resp_http_headers)

        self.writer.write_request_response_pair(request, response)
        self.maybe_close()
        LOGGER.debug('wrote warc request-response pair'+p(self.prefix), 'for url', url)
        stats.stats_sum('warc r/r'+p(self.prefix), 1)


def headers_to_str_headers(headers):
    '''
    Converts dict or tuple-based headers of bytes or str to
    tuple-based headers of str, which is the python norm (pep 3333)
    '''
    ret = []

    if isinstance(headers, collections_abc.Mapping):
        h = headers.items()
    else:
        h = headers

    for tup in h:
        k, v = tup
        if isinstance(k, six.binary_type):
            k = k.decode('iso-8859-1')
        if isinstance(v, six.binary_type):
            v = v.decode('iso-8859-1')
        ret.append((k, v))
    return ret


def p(prefix):
    if prefix:
        return ' (prefix '+prefix+')'
    else:
        return ''