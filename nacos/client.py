import base64
import hashlib
import logging
import socket
import json
import platform

try:
    import ssl
except ImportError:
    ssl = None

from multiprocessing import Process, Manager, Queue, pool
from threading import RLock, Thread
import time

try:
    # python3.6
    from http import HTTPStatus
    from urllib.request import Request, urlopen
    from urllib.parse import urlencode, unquote_plus, quote
    from urllib.error import HTTPError, URLError
except ImportError:
    # python2.7
    import httplib as HTTPStatus
    from urllib2 import Request, urlopen, HTTPError, URLError
    from urllib import urlencode, unquote_plus, quote

    base64.encodebytes = base64.encodestring

from .commons import synchronized_with_attr, truncate, python_version_bellow
from .params import group_key, parse_key, is_valid
from .files import read_file_str, save_file, delete_file
from .exception import NacosException, NacosRequestException

logging.basicConfig()
logger = logging.getLogger()

DEBUG = False
VERSION = "0.1.1"

DEFAULT_GROUP_NAME = "DEFAULT_GROUP"
DEFAULT_NAMESPACE = ""

WORD_SEPARATOR = u'\x02'
LINE_SEPARATOR = u'\x01'

DEFAULTS = {
    "APP_NAME": "Nacos-SDK-Python",
    "TIMEOUT": 3,  # in seconds
    "PULLING_TIMEOUT": 30,  # in seconds
    "PULLING_CONFIG_SIZE": 3000,
    "CALLBACK_THREAD_NUM": 10,
    "FAILOVER_BASE": "nacos-data/data",
    "SNAPSHOT_BASE": "nacos-data/snapshot",
}

OPTIONS = set(
    ["default_timeout", "pulling_timeout", "pulling_config_size", "callback_thread_num",
     "failover_base", "snapshot_base", "no_snapshot"])


def process_common_config_params(data_id, group):
    if not group or not group.strip():
        group = DEFAULT_GROUP_NAME
    else:
        group = group.strip()

    if not data_id or not is_valid(data_id):
        raise NacosException("Invalid dataId.")

    if not is_valid(group):
        raise NacosException("Invalid group.")
    return data_id, group


def parse_pulling_result(result):
    if not result:
        return list()
    ret = list()
    for i in unquote_plus(result.decode()).split(LINE_SEPARATOR):
        if not i.strip():
            continue
        sp = i.split(WORD_SEPARATOR)
        if len(sp) < 3:
            sp.append("")
        ret.append(sp)
    return ret


class WatcherWrap:
    def __init__(self, key, callback):
        self.callback = callback
        self.last_md5 = None
        self.watch_key = key


class CacheData:
    def __init__(self, key, client):
        self.key = key
        local_value = read_file_str(client.failover_base, key) or read_file_str(client.snapshot_base, key)
        self.content = local_value
        self.md5 = hashlib.md5(local_value.encode("UTF-8")).hexdigest() if local_value else None
        self.is_init = True
        if not self.md5:
            logger.debug("[init-cache] cache for %s does not have local value" % key)


def parse_nacos_server_addr(server_addr):
    sp = server_addr.split(":")
    port = int(sp[1]) if len(sp) > 1 else 8848
    return sp[0], port


class NacosClient:
    debug = False

    @staticmethod
    def set_debugging():
        if not NacosClient.debug:
            global logger
            logger = logging.getLogger("nacos")
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s:%(message)s"))
            logger.addHandler(handler)
            logger.setLevel(logging.DEBUG)
            NacosClient.debug = True

    def __init__(self, server_addresses, endpoint=None, namespace=None, ak=None, sk=None):
        self.server_list = list()

        try:
            for server_addr in server_addresses.split(","):
                self.server_list.append(parse_nacos_server_addr(server_addr.strip()))
        except:
            logger.exception("[init] bad server address for %s" % server_addresses)
            raise

        self.current_server = self.server_list[0]

        self.endpoint = endpoint
        self.namespace = namespace or DEFAULT_NAMESPACE or ""
        self.ak = ak
        self.sk = sk

        self.server_list_lock = RLock()
        self.server_offset = 0

        self.watcher_mapping = dict()
        self.pulling_lock = RLock()
        self.puller_mapping = None
        self.notify_queue = None
        self.callback_tread_pool = None
        self.process_mgr = None

        self.default_timeout = DEFAULTS["TIMEOUT"]
        self.auth_enabled = self.ak and self.sk
        self.cai_enabled = True
        self.pulling_timeout = DEFAULTS["PULLING_TIMEOUT"]
        self.pulling_config_size = DEFAULTS["PULLING_CONFIG_SIZE"]
        self.callback_tread_num = DEFAULTS["CALLBACK_THREAD_NUM"]
        self.failover_base = DEFAULTS["FAILOVER_BASE"]
        self.snapshot_base = DEFAULTS["SNAPSHOT_BASE"]
        self.no_snapshot = False

        logger.info("[client-init] endpoint:%s, tenant:%s" % (endpoint, namespace))

    def set_options(self, **kwargs):
        for k, v in kwargs.items():
            if k not in OPTIONS:
                logger.warning("[set_options] unknown option:%s, ignored" % k)
                continue

            logger.debug("[set_options] key:%s, value:%s" % (k, v))
            setattr(self, k, v)

    def change_server(self):
        with self.server_list_lock:
            self.server_offset = (self.server_offset + 1) % len(self.server_list)
            self.current_server = self.server_list[self.server_offset]

    def get_server(self):
        logger.info("[get-server] use server:%s" % str(self.current_server))
        return self.current_server

    def remove_config(self, data_id, group, timeout=None):
        data_id, group = process_common_config_params(data_id, group)
        logger.info(
            "[remove] data_id:%s, group:%s, namespace:%s, timeout:%s" % (data_id, group, self.namespace, timeout))

        params = {
            "dataId": data_id,
            "group": group,
        }
        if self.namespace:
            params["tenant"] = self.namespace

        try:
            resp = self._do_sync_req("/nacos/v1/cs/configs", None, None, params,
                                     timeout or self.default_timeout, "DELETE")
            c = resp.read()
            logger.info("[remove] remove group:%s, data_id:%s, server response:%s" % (
                group, data_id, c))
            return c == b"true"
        except HTTPError as e:
            if e.code == HTTPStatus.FORBIDDEN:
                logger.error(
                    "[remove] no right for namespace:%s, group:%s, data_id:%s" % (self.namespace, group, data_id))
                raise NacosException("Insufficient privilege.")
            else:
                logger.error("[remove] error code [:%s] for namespace:%s, group:%s, data_id:%s" % (
                    e.code, self.namespace, group, data_id))
                raise NacosException("Request Error, code is %s" % e.code)
        except Exception as e:
            logger.exception("[remove] exception %s occur" % str(e))
            raise

    def publish_config(self, data_id, group, content, timeout=None):
        if content is None:
            raise NacosException("Can not publish none content, use remove instead.")

        data_id, group = process_common_config_params(data_id, group)
        if type(content) == bytes:
            content = content.decode("UTF-8")

        logger.info("[publish] data_id:%s, group:%s, namespace:%s, content:%s, timeout:%s" % (
            data_id, group, self.namespace, truncate(content), timeout))

        params = {
            "dataId": data_id,
            "group": group,
            "content": content.encode("UTF-8"),
        }

        if self.namespace:
            params["tenant"] = self.namespace

        try:
            resp = self._do_sync_req("/nacos/v1/cs/configs", None, None, params,
                                     timeout or self.default_timeout, "POST")
            c = resp.read()
            logger.info("[publish] publish content, group:%s, data_id:%s, server response:%s" % (
                group, data_id, c))
            return c == b"true"
        except HTTPError as e:
            if e.code == HTTPStatus.FORBIDDEN:
                raise NacosException("Insufficient privilege.")
            else:
                raise NacosException("Request Error, code is %s" % e.code)
        except Exception as e:
            logger.exception("[publish] exception %s occur" % str(e))
            raise

    def get_config(self, data_id, group, timeout=None, no_snapshot=None):
        no_snapshot = self.no_snapshot if no_snapshot is None else no_snapshot
        data_id, group = process_common_config_params(data_id, group)
        logger.info("[get-config] data_id:%s, group:%s, namespace:%s, timeout:%s" % (
            data_id, group, self.namespace, timeout))

        params = {
            "dataId": data_id,
            "group": group,
        }
        if self.namespace:
            params["tenant"] = self.namespace

        cache_key = group_key(data_id, group, self.namespace)
        # get from failover
        content = read_file_str(self.failover_base, cache_key)
        if content is None:
            logger.debug("[get-config] failover config is not exist for %s, try to get from server" % cache_key)
        else:
            logger.debug("[get-config] get %s from failover directory, content is %s" % (cache_key, truncate(content)))
            return content

        # get from server
        try:
            resp = self._do_sync_req("/nacos/v1/cs/configs", None, params, None, timeout or self.default_timeout)
            content = resp.read().decode("UTF-8")
        except HTTPError as e:
            if e.code == HTTPStatus.NOT_FOUND:
                logger.warning(
                    "[get-config] config not found for data_id:%s, group:%s, namespace:%s, try to delete snapshot" % (
                        data_id, group, self.namespace))
                delete_file(self.snapshot_base, cache_key)
                return None
            elif e.code == HTTPStatus.CONFLICT:
                logger.error(
                    "[get-config] config being modified concurrently for data_id:%s, group:%s, namespace:%s" % (
                        data_id, group, self.namespace))
            elif e.code == HTTPStatus.FORBIDDEN:
                logger.error("[get-config] no right for data_id:%s, group:%s, namespace:%s" % (
                    data_id, group, self.namespace))
                raise NacosException("Insufficient privilege.")
            else:
                logger.error("[get-config] error code [:%s] for data_id:%s, group:%s, namespace:%s" % (
                    e.code, data_id, group, self.namespace))
                if no_snapshot:
                    raise
        except Exception as e:
            logger.exception("[get-config] exception %s occur" % str(e))
            if no_snapshot:
                raise

        if no_snapshot:
            return content

        if content is not None:
            logger.info(
                "[get-config] content from server:%s, data_id:%s, group:%s, namespace:%s, try to save snapshot" % (
                    truncate(content), data_id, group, self.namespace))
            try:
                save_file(self.snapshot_base, cache_key, content)
            except Exception as e:
                logger.exception("[get-config] save snapshot failed for %s, data_id:%s, group:%s, namespace:%s" % (
                    data_id, group, self.namespace, str(e)))
            return content

        logger.error("[get-config] get config from server failed, try snapshot, data_id:%s, group:%s, namespace:%s" % (
            data_id, group, self.namespace))
        content = read_file_str(self.snapshot_base, cache_key)
        if content is None:
            logger.warning("[get-config] snapshot is not exist for %s." % cache_key)
        else:
            logger.debug("[get-config] get %s from snapshot directory, content is %s" % (cache_key, truncate(content)))
            return content

    @synchronized_with_attr("pulling_lock")
    def add_config_watcher(self, data_id, group, cb):
        self.add_config_watchers(data_id, group, [cb])

    @synchronized_with_attr("pulling_lock")
    def add_config_watchers(self, data_id, group, cb_list):
        if not cb_list:
            raise NacosException("A callback function is needed.")
        data_id, group = process_common_config_params(data_id, group)
        logger.info("[add-watcher] data_id:%s, group:%s, namespace:%s" % (data_id, group, self.namespace))
        cache_key = group_key(data_id, group, self.namespace)
        wl = self.watcher_mapping.get(cache_key)
        if not wl:
            wl = list()
            self.watcher_mapping[cache_key] = wl
        for cb in cb_list:
            wl.append(WatcherWrap(cache_key, cb))
            logger.info("[add-watcher] watcher has been added for key:%s, new callback is:%s, callback number is:%s" % (
                cache_key, cb.__name__, len(wl)))

        if self.puller_mapping is None:
            logger.debug("[add-watcher] pulling should be initialized")
            self._init_pulling()

        if cache_key in self.puller_mapping:
            logger.debug("[add-watcher] key:%s is already in pulling" % cache_key)
            return

        for key, puller_info in self.puller_mapping.items():
            if len(puller_info[1]) < self.pulling_config_size:
                logger.debug("[add-watcher] puller:%s is available, add key:%s" % (puller_info[0], cache_key))
                puller_info[1].append(cache_key)
                self.puller_mapping[cache_key] = puller_info
                break
        else:
            logger.debug("[add-watcher] no puller available, new one and add key:%s" % cache_key)
            key_list = self.process_mgr.list()
            key_list.append(cache_key)
            sys_os = platform.system()
            if sys_os == 'Windows':
                puller = Thread(target=self._do_pulling, args=(key_list, self.notify_queue))
                puller.setDaemon(True)
            else:
                puller = Process(target=self._do_pulling, args=(key_list, self.notify_queue))
                puller.daemon = True
            puller.start()
            self.puller_mapping[cache_key] = (puller, key_list)

    @synchronized_with_attr("pulling_lock")
    def remove_config_watcher(self, data_id, group, cb, remove_all=False):
        if not cb:
            raise NacosException("A callback function is needed.")
        data_id, group = process_common_config_params(data_id, group)
        if not self.puller_mapping:
            logger.warning("[remove-watcher] watcher is never started.")
            return
        cache_key = group_key(data_id, group, self.namespace)
        wl = self.watcher_mapping.get(cache_key)
        if not wl:
            logger.warning("[remove-watcher] there is no watcher on key:%s" % cache_key)
            return

        wrap_to_remove = list()
        for i in wl:
            if i.callback == cb:
                wrap_to_remove.append(i)
                if not remove_all:
                    break

        for i in wrap_to_remove:
            wl.remove(i)

        logger.info("[remove-watcher] %s is removed from %s, remove all:%s" % (cb.__name__, cache_key, remove_all))
        if not wl:
            logger.debug("[remove-watcher] there is no watcher for:%s, kick out from pulling" % cache_key)
            self.watcher_mapping.pop(cache_key)
            puller_info = self.puller_mapping[cache_key]
            puller_info[1].remove(cache_key)
            if not puller_info[1]:
                logger.debug("[remove-watcher] there is no pulling keys for puller:%s, stop it" % puller_info[0])
                self.puller_mapping.pop(cache_key)
                puller_info[0].terminate()

    def _do_sync_req(self, url, headers=None, params=None, data=None, timeout=None, method="GET"):
        url = "?".join([url, urlencode(params)]) if params else url
        all_headers = self._get_common_headers(params, data)
        if headers:
            all_headers.update(headers)
        logger.debug(
            "[do-sync-req] url:%s, headers:%s, params:%s, data:%s, timeout:%s" % (
                url, all_headers, params, data, timeout))
        tries = 0
        while True:
            try:
                server_info = self.get_server()
                if not server_info:
                    logger.error("[do-sync-req] can not get one server.")
                    raise NacosRequestException("Server is not available.")
                address, port = server_info
                server = ":".join([address, str(port)])
                server_url = "%s://%s" % ("http", server)
                if python_version_bellow("3"):
                    req = Request(url=server_url + url, data=urlencode(data).encode() if data else None,
                                  headers=all_headers)
                    req.get_method = lambda: method
                else:
                    req = Request(url=server_url + url, data=urlencode(data).encode() if data else None,
                                  headers=all_headers, method=method)

                # for python version compatibility
                if python_version_bellow("2.7.9"):
                    resp = urlopen(req, timeout=timeout)
                else:
                    resp = urlopen(req, timeout=timeout, context=None)
                logger.debug("[do-sync-req] info from server:%s" % server)
                return resp
            except HTTPError as e:
                if e.code in [HTTPStatus.INTERNAL_SERVER_ERROR, HTTPStatus.BAD_GATEWAY,
                              HTTPStatus.SERVICE_UNAVAILABLE]:
                    logger.warning("[do-sync-req] server:%s is not available for reason:%s" % (server, e.msg))
                else:
                    raise
            except socket.timeout:
                logger.warning("[do-sync-req] %s request timeout" % server)
            except URLError as e:
                logger.warning("[do-sync-req] %s connection error:%s" % (server, e.reason))

            tries += 1
            if tries >= len(self.server_list):
                logger.error("[do-sync-req] %s maybe down, no server is currently available" % server)
                raise NacosRequestException("All server are not available")
            self.change_server()
            logger.warning("[do-sync-req] %s maybe down, skip to next" % server)

    def _do_pulling(self, cache_list, queue):
        cache_pool = dict()
        for cache_key in cache_list:
            cache_pool[cache_key] = CacheData(cache_key, self)

        while cache_list:
            unused_keys = set(cache_pool.keys())
            contains_init_key = False
            probe_update_string = ""
            for cache_key in cache_list:
                cache_data = cache_pool.get(cache_key)
                if not cache_data:
                    logger.debug("[do-pulling] new key added: %s" % cache_key)
                    cache_data = CacheData(cache_key, self)
                    cache_pool[cache_key] = cache_data
                else:
                    unused_keys.remove(cache_key)
                if cache_data.is_init:
                    contains_init_key = True
                data_id, group, namespace = parse_key(cache_key)
                probe_update_string += WORD_SEPARATOR.join(
                    [data_id, group, cache_data.md5 or "", self.namespace]) + LINE_SEPARATOR

            for k in unused_keys:
                logger.debug("[do-pulling] %s is no longer watched, remove from cache" % k)
                cache_pool.pop(k)

            logger.debug(
                "[do-pulling] try to detected change from server probe string is %s" % truncate(probe_update_string))
            headers = {"Long-Pulling-Timeout": int(self.pulling_timeout * 1000)}
            # if contains_init_key:
            #     headers["longPullingNoHangUp"] = "true"

            data = {"Listening-Configs": probe_update_string}

            changed_keys = list()
            try:
                resp = self._do_sync_req("/nacos/v1/cs/configs/listener", headers, None, data,
                                         self.pulling_timeout + 10, "POST")
                changed_keys = [group_key(*i) for i in parse_pulling_result(resp.read())]
                logger.debug("[do-pulling] following keys are changed from server %s" % truncate(str(changed_keys)))
            except NacosException as e:
                logger.error("[do-pulling] nacos exception: %s, waiting for recovery" % str(e))
                time.sleep(1)
            except Exception as e:
                logger.exception("[do-pulling] exception %s occur, return empty list, waiting for recovery" % str(e))
                time.sleep(1)

            for cache_key, cache_data in cache_pool.items():
                cache_data.is_init = False
                if cache_key in changed_keys:
                    data_id, group, namespace = parse_key(cache_key)
                    content = self.get_config(data_id, group)
                    md5 = hashlib.md5(content.encode("UTF-8")).hexdigest() if content is not None else None
                    cache_data.md5 = md5
                    cache_data.content = content
                queue.put((cache_key, cache_data.content, cache_data.md5))

    @synchronized_with_attr("pulling_lock")
    def _init_pulling(self):
        if self.puller_mapping is not None:
            logger.info("[init-pulling] puller is already initialized")
            return
        self.puller_mapping = dict()
        self.notify_queue = Queue()
        self.callback_tread_pool = pool.ThreadPool(self.callback_tread_num)
        self.process_mgr = Manager()
        t = Thread(target=self._process_polling_result)
        t.setDaemon(True)
        t.start()
        logger.info("[init-pulling] init completed")

    def _process_polling_result(self):
        while True:
            cache_key, content, md5 = self.notify_queue.get()
            logger.debug("[process-polling-result] receive an event:%s" % cache_key)
            wl = self.watcher_mapping.get(cache_key)
            if not wl:
                logger.warning("[process-polling-result] no watcher on %s, ignored" % cache_key)
                continue

            data_id, group, namespace = parse_key(cache_key)
            plain_content = content

            params = {
                "data_id": data_id,
                "group": group,
                "namespace": namespace,
                "raw_content": content,
                "content": plain_content,
            }
            for watcher in wl:
                if not watcher.last_md5 == md5:
                    logger.debug(
                        "[process-polling-result] md5 changed since last call, calling %s" % watcher.callback.__name__)
                    try:
                        self.callback_tread_pool.apply(watcher.callback, (params,))
                    except Exception as e:
                        logger.exception("[process-polling-result] exception %s occur while calling %s " % (
                            str(e), watcher.callback.__name__))
                    watcher.last_md5 = md5

    def _get_common_headers(self, params, data):
        return {}

    def add_naming_instance(self, service_name, ip, port, cluster_name=None, weight=1.0, metadata=None,
                            enable=True, healthy=True):
        logger.info("[add-naming-instance] ip:%s, port:%s, service_name:%s, namespace:%s" % (
            ip, port, service_name, self.namespace))

        params = {
            "ip": ip,
            "port": port,
            "serviceName": service_name,
            "weight": weight,
            "enable": enable,
            "healthy": healthy,
            "metadata": metadata,
            "clusterName": cluster_name
        }

        if self.namespace:
            params["namespaceId"] = self.namespace

        try:
            resp = self._do_sync_req("/nacos/v1/ns/instance", None, None, params, self.default_timeout, "POST")
            c = resp.read()
            logger.info("[add-naming-instance] ip:%s, port:%s, service_name:%s, namespace:%s, server response:%s" % (
                ip, port, service_name, self.namespace, c))
            return c == b"ok"
        except HTTPError as e:
            if e.code == HTTPStatus.FORBIDDEN:
                raise NacosException("Insufficient privilege.")
            else:
                raise NacosException("Request Error, code is %s" % e.code)
        except Exception as e:
            logger.exception("[add-naming-instance] exception %s occur" % str(e))
            raise

    def remove_naming_instance(self, service_name, ip, port, cluster_name=None):
        logger.info("[remove-naming-instance] ip:%s, port:%s, service_name:%s, namespace:%s" % (
            ip, port, service_name, self.namespace))

        params = {
            "ip": ip,
            "port": port,
            "serviceName": service_name,
        }

        if cluster_name is not None:
            params["clusterName"] = cluster_name

        if self.namespace:
            params["namespaceId"] = self.namespace

        try:
            resp = self._do_sync_req("/nacos/v1/ns/instance", None, None, params, self.default_timeout, "DELETE")
            c = resp.read()
            logger.info("[remove-naming-instance] ip:%s, port:%s, service_name:%s, namespace:%s, server response:%s" % (
                ip, port, service_name, self.namespace, c))
            return c == b"ok"
        except HTTPError as e:
            if e.code == HTTPStatus.FORBIDDEN:
                raise NacosException("Insufficient privilege.")
            else:
                raise NacosException("Request Error, code is %s" % e.code)
        except Exception as e:
            logger.exception("[remove-naming-instance] exception %s occur" % str(e))
            raise

    def modify_naming_instance(self, service_name, ip, port, cluster_name=None, weight=None, metadata=None,
                               enable=None):
        logger.info("[modify-naming-instance] ip:%s, port:%s, service_name:%s, namespace:%s" % (
            ip, port, service_name, self.namespace))

        params = {
            "ip": ip,
            "port": port,
            "serviceName": service_name,
        }

        if cluster_name is not None:
            params["clusterName"] = cluster_name

        if enable is not None:
            params["enable"] = enable

        if weight is not None:
            params["weight"] = weight

        if metadata is not None:
            params["metadata"] = metadata

        if self.namespace:
            params["namespaceId"] = self.namespace

        try:
            resp = self._do_sync_req("/nacos/v1/ns/instance", None, None, params, self.default_timeout, "PUT")
            c = resp.read()
            logger.info("[modify-naming-instance] ip:%s, port:%s, service_name:%s, namespace:%s, server response:%s" % (
                ip, port, service_name, self.namespace, c))
            return c == b"ok"
        except HTTPError as e:
            if e.code == HTTPStatus.FORBIDDEN:
                raise NacosException("Insufficient privilege.")
            else:
                raise NacosException("Request Error, code is %s" % e.code)
        except Exception as e:
            logger.exception("[modify-naming-instance] exception %s occur" % str(e))
            raise

    def list_naming_instance(self, service_name, clusters=None, healthy_only=False):
        logger.info("[list-naming-instance] service_name:%s, namespace:%s" % (service_name, self.namespace))

        params = {
            "serviceName": service_name,
            "healthyOnly": healthy_only
        }

        if clusters is not None:
            params["clusters"] = clusters

        if self.namespace:
            params["namespaceId"] = self.namespace

        try:
            resp = self._do_sync_req("/nacos/v1/ns/instance/list", None, params, None, self.default_timeout, "GET")
            c = resp.read()
            logger.info("[list-naming-instance] service_name:%s, namespace:%s, server response:%s" %
                        (service_name, self.namespace, c))
            return json.loads(c.decode("UTF-8"))
        except HTTPError as e:
            if e.code == HTTPStatus.FORBIDDEN:
                raise NacosException("Insufficient privilege.")
            else:
                raise NacosException("Request Error, code is %s" % e.code)
        except Exception as e:
            logger.exception("[list-naming-instance] exception %s occur" % str(e))
            raise

    def get_naming_instance(self, service_name, ip, port, cluster_name=None):
        logger.info("[get-naming-instance] ip:%s, port:%s, service_name:%s, namespace:%s" % (ip, port, service_name,
                                                                                             self.namespace))

        params = {
            "serviceName": service_name,
            "ip": ip,
            "port": port,
        }

        if cluster_name is not None:
            params["cluster"] = cluster_name

        if self.namespace:
            params["namespaceId"] = self.namespace

        try:
            resp = self._do_sync_req("/nacos/v1/ns/instance", None, params, None, self.default_timeout, "GET")
            c = resp.read()
            logger.info("[get-naming-instance] ip:%s, port:%s, service_name:%s, namespace:%s, server response:%s" %
                        (ip, port, service_name, self.namespace, c))
            return json.loads(c.decode("UTF-8"))
        except HTTPError as e:
            if e.code == HTTPStatus.FORBIDDEN:
                raise NacosException("Insufficient privilege.")
            else:
                raise NacosException("Request Error, code is %s" % e.code)
        except Exception as e:
            logger.exception("[get-naming-instance] exception %s occur" % str(e))
            raise

    def send_heartbeat(self, service_name, ip, port, cluster_name=None, weight=1.0, metadata=None):
        logger.info("[send-heartbeat] ip:%s, port:%s, service_name:%s, namespace:%s" % (ip, port, service_name,
                                                                                        self.namespace))
        beat_data = {
            "serviceName": service_name,
            "ip": ip,
            "port": port,
            "weight": weight
        }

        if cluster_name is not None:
            beat_data["cluster"] = cluster_name

        if metadata is not None:
            beat_data["metadata"] = json.loads(metadata)

        params = {
            "serviceName": service_name,
            "beat": json.dumps(beat_data),
        }

        if self.namespace:
            params["namespaceId"] = self.namespace

        try:
            resp = self._do_sync_req("/nacos/v1/ns/instance/beat", None, params, None, self.default_timeout, "PUT")
            c = resp.read()
            logger.info("[send-heartbeat] ip:%s, port:%s, service_name:%s, namespace:%s, server response:%s" %
                        (ip, port, service_name, self.namespace, c))
            return json.loads(c.decode("UTF-8"))
        except HTTPError as e:
            if e.code == HTTPStatus.FORBIDDEN:
                raise NacosException("Insufficient privilege.")
            else:
                raise NacosException("Request Error, code is %s" % e.code)
        except Exception as e:
            logger.exception("[send-heartbeat] exception %s occur" % str(e))
            raise


if DEBUG:
    NacosClient.set_debugging()
