#!/usr/bin/env python

import argparse
import hashlib
import json
import logging
import os
import requests
import requests_toolbelt
import sys
import uuid
from progressbar import Bar, ETA, FileTransferSpeed, Percentage, ProgressBar


class CloudNode(object):
    def __init__(self, node):
        self.node = node
        self._children_fetched = False

    def children(self):
        if not self._children_fetched:
            nodes = DriveSink.instance().request_metadata(
                "%%snodes/%s/children" % self.node["id"])
            self._children = {n["name"]: CloudNode(n) for n in nodes["data"]}
            self._children_fetched = True
        return self._children

    def child(self, name, create=False):
        node = self.children().get(name)
        if not node and create:
            node = self._make_child_folder(name)
        return node

    def upload_child_file(self, name, local_path, existing_node=None):
        #logging.info("Uploading %s to %s", local_path, self.node["name"])

        e = requests_toolbelt.MultipartEncoder([
            ("metadata", json.dumps({
                "name": name,
                "kind": "FILE",
                "parents": [self.node["id"]],
            })),
            ("content", (name, open(local_path, "rb")))])
        
        pbar = ProgressBar(widgets=[local_path, ' ', FileTransferSpeed(), ' ', Percentage(),
                                    Bar(), ETA(), ' '],
                           maxval=len(e)).start()

        def progress_cb(monitor):
          if pbar.maxval == monitor.bytes_read:
              pbar.finish()
          else:
              pbar.update(monitor.bytes_read)

        m = requests_toolbelt.MultipartEncoderMonitor(e, progress_cb)
        if existing_node:
            """
            # TODO: this is under-documented and currently 500s on Amazon's side
            node = CloudNode(DriveSink.instance().request_content(
                "%%snodes/%s/content" % existing_node.node["id"],
                method="put", data=m, headers={"Content-Type": m.content_type}))
            """
            old_info = DriveSink.instance().request_metadata(
                "%%s/trash/%s" % existing_node.node["id"], method="put")
        node = CloudNode(DriveSink.instance().request_content(
            "%snodes", method="post", data=m,
            headers={"Content-Type": m.content_type}))
        self._children[name] = node

    def download_file(self, local_path):
        logging.info("Downloading %s into %s", self.node["name"], local_path)
        req = DriveSink.instance().request_content(
            "%%snodes/%s/content" % self.node["id"], method="get", stream=True,
            decode=False)
        if req.status_code != 200:
            logging.error("Unable to download file: %r", req.text)
            sys.exit(1)
        with open(local_path, "wb") as f:
            for chunk in req.iter_content():
                f.write(chunk)

    def differs(self, local_path):
        return (not os.path.exists(local_path) or
                self.node["contentProperties"]["size"] !=
                os.path.getsize(local_path) or
                self.node["contentProperties"]["md5"] !=
                self._md5sum(local_path))

    def _md5sum(self, filename, blocksize=65536):
        md5 = hashlib.md5()
        with open(filename, "rb") as f:
            for block in iter(lambda: f.read(blocksize), ""):
                md5.update(block)
        return md5.hexdigest()

    def _make_child_folder(self, name):
        logging.info(
            "Creating remote folder %s in %s", name, self.node["name"])
        node = CloudNode(
            DriveSink.instance().request_metadata("%snodes", {
                "kind": "FOLDER",
                "name": name,
                "parents": [self.node["id"]]}))
        self._children[name] = node
        return node


class DriveSink(object):
    def __init__(self, args):
        if not args:
            logging.error("Never initialized")
            sys.exit(1)
        self.args = args
        self.config = None

    @classmethod
    def instance(cls, args=None):
        if not hasattr(cls, "_instance"):
            cls._instance = cls(args)
        return cls._instance

    def upload(self, source, destination):
        remote_node = self.node_at_path(
            self.get_root(), destination, create_missing=True)
        for dirpath, dirnames, filenames in os.walk(source):
            relative = dirpath[len(source):]
            current_dir = self.node_at_path(
                remote_node, relative, create_missing=True)
            if not current_dir:
                logging.error("Could not create missing node")
                sys.exit(1)
            for dirname in dirnames:
                current_dir.child(dirname, create=True)
            for filename in filenames:
                local_path = os.path.join(dirpath, filename)
                node = current_dir.child(filename)
                if (not node or node.differs(
                        local_path)) and self.filter_file(filename):
                    current_dir.upload_child_file(filename, local_path, node)

    def download(self, source, destination):
        to_download = [(self.node_at_path(self.get_root(), source),
                        self.join_path(destination, create_missing=True))]
        while len(to_download):
            node, path = to_download.pop(0)
            for name, child in node.children().iteritems():
                if child.node["kind"] == "FOLDER":
                    to_download.append((child, self.join_path(
                        child.node["name"], path, create_missing=True)))
                elif child.node["kind"] == "FILE":
                    local_path = os.path.join(path, child.node["name"])
                    if child.differs(local_path):
                        child.download_file(local_path)

    def filter_file(self, filename):
        _, extension = os.path.splitext(filename)
        extension = extension.lstrip(".").lower()

        allowed = self.args.extensions
        if not allowed:
            # Not all tested to be free
            allowed = (
                "apng,arw,bmp,cr2,crw,dng,emf,gif,jfif,jpe,jpeg,jpg,mef,nef,"
                "orf,pcx,png,psd,raf,ras,srw,swf,tga,tif,tiff,wmf")
        return extension in allowed.split(",")

    def get_root(self):
        nodes = self.request_metadata("%snodes?filters=isRoot:true")
        if nodes["count"] != 1:
            logging.error("Could not find root")
            sys.exit(1)
        return CloudNode(nodes["data"][0])

    def node_at_path(self, root, path, create_missing=False):
        parts = filter(None, path.split("/"))
        node = root
        while len(parts):
            node = node.child(parts.pop(0), create=create_missing)
            if not node:
                return None
        return node

    def join_path(self, destination, root="/", create_missing=True):
        directory = os.path.join(root, destination)
        if not os.path.exists(directory):
            if create_missing:
                os.makedirs(directory)
            else:
                return None
        if not os.path.isdir(directory):
            logging.error("%s is not a directory", directory)
            sys.exit(1)
        return directory

    def _config_file(self):
        config_filename = self.args.config or os.environ.get(
            "DRIVESINK", None)
        if not config_filename:
            config_filename = os.path.join(
                os.path.expanduser("~"), ".drivesink")
        return config_filename

    def _config(self):
        if not self.config:
            config_filename = self._config_file()
            try:
                self.config = json.loads(open(config_filename, "r").read())
            except:
                print "%s/config to get your tokens" % self.args.drivesink
                sys.exit(1)
        return self.config

    def request_metadata(self, path, json_data=None, **kwargs):
        args = {}
        if json_data:
            args["method"] = "post"
            args["data"] = json.dumps(json_data)
        else:
            args["method"] = "get"

        args.update(kwargs)

        return self._request(
            path % self._config()["metadataUrl"], **args)

    def request_content(self, path, **kwargs):
        return self._request(
            path % self._config()["contentUrl"], **kwargs)

    def _request(self, url, refresh=True, decode=True, **kwargs):
        headers = {
            "Authorization": "Bearer %s" % self._config()["access_token"],
        }
        headers.update(kwargs.pop("headers", {}))
        req = requests.request(url=url, headers=headers, **kwargs)
        if req.status_code == 401 and refresh:
            # Have to proxy to get the client id and secret
            req = requests.post("%s/refresh" % self.args.drivesink, data={
                "refresh_token": self._config()["refresh_token"],
            })
            if req.status_code != 200:
                try:
                    response = req.json()
                    logging.error("Got Amazon code %s: %s",
                                  response["code"], response["message"])
                    sys.exit(1)
                except Exception:
                    pass
            req.raise_for_status()
            try:
                new_config = req.json()
            except:
                logging.error("Could not refresh: %r", req.text)
                raise
            self.config.update(new_config)
            with open(self._config_file(), "w") as f:
                f.write(json.dumps(self.config, sort_keys=True, indent=4))
            return self._request(url, refresh=False, decode=decode, **kwargs)
        if req.status_code != 200:
            try:
                response = req.json()
                logging.error("Got Amazon code %s: %s",
                              response["code"], response["message"])
                sys.exit(1)
            except Exception:
                pass
        req.raise_for_status()
        if decode:
            return req.json()
        return req


def main():
    parser = argparse.ArgumentParser(
        description="Amazon Cloud Drive synchronization tool")
    parser.add_argument("command", choices=["upload", "download"],
                        help="Commands: 'upload' or 'download'")
    parser.add_argument("source", help="The source directory")
    parser.add_argument("destination", help="The destination directory")
    parser.add_argument("-e", "--extensions",
                        help="File extensions to upload, images by default")
    parser.add_argument("-c", "--config", help="The config file")
    parser.add_argument("-d", "--drivesink", help="Drivesink URL",
                        default="https://drivesink.appspot.com")
    args = parser.parse_args()

    drivesink = DriveSink.instance(args)

    if args.command == "upload":
        drivesink.upload(args.source, args.destination)
    elif args.command == "download":
        drivesink.download(args.source, args.destination)

logging.basicConfig(
    format = "%(levelname) -10s %(module)s:%(lineno)s %(funcName)s %(message)s",
    level = logging.DEBUG
)
logging.getLogger("requests").setLevel(logging.WARNING)

if __name__ == "__main__":
    main()
