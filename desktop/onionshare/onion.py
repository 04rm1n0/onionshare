# -*- coding: utf-8 -*-
"""
OnionShare | https://onionshare.org/

Copyright (C) 2014-2020 Micah Lee, et al. <micah@micahflee.com>

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

from stem.control import Controller
from stem import ProtocolError, SocketClosed
from stem.connection import MissingPassword, UnreadableCookieFile, AuthenticationFailure
from Crypto.PublicKey import RSA
import base64, os, sys, tempfile, shutil, urllib, platform, subprocess, time, shlex

from distutils.version import LooseVersion as Version
from . import common, strings
from .settings import Settings


class TorErrorAutomatic(Exception):
    """
    OnionShare is failing to connect and authenticate to the Tor controller,
    using automatic settings that should work with Tor Browser.
    """

    pass


class TorErrorInvalidSetting(Exception):
    """
    This exception is raised if the settings just don't make sense.
    """

    pass


class TorErrorSocketPort(Exception):
    """
    OnionShare can't connect to the Tor controller using the supplied address and port.
    """

    pass


class TorErrorSocketFile(Exception):
    """
    OnionShare can't connect to the Tor controller using the supplied socket file.
    """

    pass


class TorErrorMissingPassword(Exception):
    """
    OnionShare connected to the Tor controller, but it requires a password.
    """

    pass


class TorErrorUnreadableCookieFile(Exception):
    """
    OnionShare connected to the Tor controller, but your user does not have permission
    to access the cookie file.
    """

    pass


class TorErrorAuthError(Exception):
    """
    OnionShare connected to the address and port, but can't authenticate. It's possible
    that a Tor controller isn't listening on this port.
    """

    pass


class TorErrorProtocolError(Exception):
    """
    This exception is raised if onionshare connects to the Tor controller, but it
    isn't acting like a Tor controller (such as in Whonix).
    """

    pass


class TorTooOld(Exception):
    """
    This exception is raised if onionshare needs to use a feature of Tor or stem
    (like stealth ephemeral onion services) but the version you have installed
    is too old.
    """

    pass


class BundledTorNotSupported(Exception):
    """
    This exception is raised if onionshare is set to use the bundled Tor binary,
    but it's not supported on that platform, or in dev mode.
    """


class BundledTorTimeout(Exception):
    """
    This exception is raised if onionshare is set to use the bundled Tor binary,
    but Tor doesn't finish connecting promptly.
    """


class BundledTorCanceled(Exception):
    """
    This exception is raised if onionshare is set to use the bundled Tor binary,
    and the user cancels connecting to Tor
    """


class BundledTorBroken(Exception):
    """
    This exception is raised if onionshare is set to use the bundled Tor binary,
    but the process seems to fail to run.
    """


class Onion(object):
    """
    Onion is an abstraction layer for connecting to the Tor control port and
    creating onion services. OnionShare supports creating onion services by
    connecting to the Tor controller and using ADD_ONION, DEL_ONION.

    stealth: Should the onion service be stealth?

    settings: A Settings object. If it's not passed in, load from disk.

    bundled_connection_func: If the tor connection type is bundled, optionally
    call this function and pass in a status string while connecting to tor. This
    is necessary for status updates to reach the GUI.
    """

    def __init__(self, common, use_tmp_dir=False):
        self.common = common
        self.common.log("Onion", "__init__")

        self.use_tmp_dir = use_tmp_dir

        # Is bundled tor supported?
        if (
            self.common.platform == "Windows" or self.common.platform == "Darwin"
        ) and getattr(sys, "onionshare_dev_mode", False):
            self.bundle_tor_supported = False
        else:
            self.bundle_tor_supported = True

        # Set the path of the tor binary, for bundled tor
        (
            self.tor_path,
            self.tor_geo_ip_file_path,
            self.tor_geo_ipv6_file_path,
            self.obfs4proxy_file_path,
        ) = self.common.get_tor_paths()

        # The tor process
        self.tor_proc = None

        # The Tor controller
        self.c = None

        # Start out not connected to Tor
        self.connected_to_tor = False

        # Assigned later if we are using stealth mode
        self.auth_string = None

    def connect(
        self,
        custom_settings=None,
        config=None,
        tor_status_update_func=None,
        connect_timeout=120,
        local_only=False,
    ):
        if local_only:
            self.common.log(
                "Onion", "connect", "--local-only, so skip trying to connect"
            )
            return

        self.common.log("Onion", "connect")

        # Either use settings that are passed in, or use them from common
        if custom_settings:
            self.settings = custom_settings
        elif config:
            self.common.load_settings(config)
            self.settings = self.common.settings
        else:
            self.common.load_settings()
            self.settings = self.common.settings

        strings.load_strings(self.common)

        # The Tor controller
        self.c = None

        if self.settings.get("connection_type") == "bundled":
            if not self.bundle_tor_supported:
                raise BundledTorNotSupported(
                    strings._("settings_error_bundled_tor_not_supported")
                )

            # Create a torrc for this session
            if self.use_tmp_dir:
                self.tor_data_directory = tempfile.TemporaryDirectory(
                    dir=self.common.build_tmp_dir()
                )
                self.tor_data_directory_name = self.tor_data_directory.name
            else:
                self.tor_data_directory_name = self.common.build_tor_dir()
            self.common.log(
                "Onion",
                "connect",
                f"tor_data_directory_name={self.tor_data_directory_name}",
            )

            # Create the torrc
            with open(self.common.get_resource_path("torrc_template")) as f:
                torrc_template = f.read()
            self.tor_cookie_auth_file = os.path.join(
                self.tor_data_directory_name, "cookie"
            )
            try:
                self.tor_socks_port = self.common.get_available_port(1000, 65535)
            except:
                raise OSError(strings._("no_available_port"))
            self.tor_torrc = os.path.join(self.tor_data_directory_name, "torrc")

            if self.common.platform == "Windows" or self.common.platform == "Darwin":
                # Windows doesn't support unix sockets, so it must use a network port.
                # macOS can't use unix sockets either because socket filenames are limited to
                # 100 chars, and the macOS sandbox forces us to put the socket file in a place
                # with a really long path.
                torrc_template += "ControlPort {{control_port}}\n"
                try:
                    self.tor_control_port = self.common.get_available_port(1000, 65535)
                except:
                    raise OSError(strings._("no_available_port"))
                self.tor_control_socket = None
            else:
                # Linux and BSD can use unix sockets
                torrc_template += "ControlSocket {{control_socket}}\n"
                self.tor_control_port = None
                self.tor_control_socket = os.path.join(
                    self.tor_data_directory_name, "control_socket"
                )

            torrc_template = torrc_template.replace(
                "{{data_directory}}", self.tor_data_directory_name
            )
            torrc_template = torrc_template.replace(
                "{{control_port}}", str(self.tor_control_port)
            )
            torrc_template = torrc_template.replace(
                "{{control_socket}}", str(self.tor_control_socket)
            )
            torrc_template = torrc_template.replace(
                "{{cookie_auth_file}}", self.tor_cookie_auth_file
            )
            torrc_template = torrc_template.replace(
                "{{geo_ip_file}}", self.tor_geo_ip_file_path
            )
            torrc_template = torrc_template.replace(
                "{{geo_ipv6_file}}", self.tor_geo_ipv6_file_path
            )
            torrc_template = torrc_template.replace(
                "{{socks_port}}", str(self.tor_socks_port)
            )

            with open(self.tor_torrc, "w") as f:
                f.write(torrc_template)

                # Bridge support
                if self.settings.get("tor_bridges_use_obfs4"):
                    f.write(
                        f"ClientTransportPlugin obfs4 exec {self.obfs4proxy_file_path}\n"
                    )
                    with open(
                        self.common.get_resource_path("torrc_template-obfs4")
                    ) as o:
                        for line in o:
                            f.write(line)
                elif self.settings.get("tor_bridges_use_meek_lite_azure"):
                    f.write(
                        f"ClientTransportPlugin meek_lite exec {self.obfs4proxy_file_path}\n"
                    )
                    with open(
                        self.common.get_resource_path("torrc_template-meek_lite_azure")
                    ) as o:
                        for line in o:
                            f.write(line)

                if self.settings.get("tor_bridges_use_custom_bridges"):
                    if "obfs4" in self.settings.get("tor_bridges_use_custom_bridges"):
                        f.write(
                            f"ClientTransportPlugin obfs4 exec {self.obfs4proxy_file_path}\n"
                        )
                    elif "meek_lite" in self.settings.get(
                        "tor_bridges_use_custom_bridges"
                    ):
                        f.write(
                            f"ClientTransportPlugin meek_lite exec {self.obfs4proxy_file_path}\n"
                        )
                    f.write(self.settings.get("tor_bridges_use_custom_bridges"))
                    f.write("\nUseBridges 1")

            # Execute a tor subprocess
            start_ts = time.time()
            if self.common.platform == "Windows":
                # In Windows, hide console window when opening tor.exe subprocess
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                self.tor_proc = subprocess.Popen(
                    [self.tor_path, "-f", self.tor_torrc],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    startupinfo=startupinfo,
                )
            else:
                self.tor_proc = subprocess.Popen(
                    [self.tor_path, "-f", self.tor_torrc],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

            # Wait for the tor controller to start
            time.sleep(2)

            # Connect to the controller
            try:
                if (
                    self.common.platform == "Windows"
                    or self.common.platform == "Darwin"
                ):
                    self.c = Controller.from_port(port=self.tor_control_port)
                    self.c.authenticate()
                else:
                    self.c = Controller.from_socket_file(path=self.tor_control_socket)
                    self.c.authenticate()
            except Exception as e:
                raise BundledTorBroken(
                    strings._("settings_error_bundled_tor_broken").format(e.args[0])
                )

            while True:
                try:
                    res = self.c.get_info("status/bootstrap-phase")
                except SocketClosed:
                    raise BundledTorCanceled()

                res_parts = shlex.split(res)
                progress = res_parts[2].split("=")[1]
                summary = res_parts[4].split("=")[1]

                # "\033[K" clears the rest of the line
                print(
                    f"\rConnecting to the Tor network: {progress}% - {summary}\033[K",
                    end="",
                )

                if callable(tor_status_update_func):
                    if not tor_status_update_func(progress, summary):
                        # If the dialog was canceled, stop connecting to Tor
                        self.common.log(
                            "Onion",
                            "connect",
                            "tor_status_update_func returned false, canceling connecting to Tor",
                        )
                        print()
                        return False

                if summary == "Done":
                    print("")
                    break
                time.sleep(0.2)

                # If using bridges, it might take a bit longer to connect to Tor
                if (
                    self.settings.get("tor_bridges_use_custom_bridges")
                    or self.settings.get("tor_bridges_use_obfs4")
                    or self.settings.get("tor_bridges_use_meek_lite_azure")
                ):
                    # Only override timeout if a custom timeout has not been passed in
                    if connect_timeout == 120:
                        connect_timeout = 150
                if time.time() - start_ts > connect_timeout:
                    print("")
                    try:
                        self.tor_proc.terminate()
                        raise BundledTorTimeout(
                            strings._("settings_error_bundled_tor_timeout")
                        )
                    except FileNotFoundError:
                        pass

        elif self.settings.get("connection_type") == "automatic":
            # Automatically try to guess the right way to connect to Tor Browser

            # Try connecting to control port
            found_tor = False

            # If the TOR_CONTROL_PORT environment variable is set, use that
            env_port = os.environ.get("TOR_CONTROL_PORT")
            if env_port:
                try:
                    self.c = Controller.from_port(port=int(env_port))
                    found_tor = True
                except:
                    pass

            else:
                # Otherwise, try default ports for Tor Browser, Tor Messenger, and system tor
                try:
                    ports = [9151, 9153, 9051]
                    for port in ports:
                        self.c = Controller.from_port(port=port)
                        found_tor = True
                except:
                    pass

                # If this still didn't work, try guessing the default socket file path
                socket_file_path = ""
                if not found_tor:
                    try:
                        if self.common.platform == "Darwin":
                            socket_file_path = os.path.expanduser(
                                "~/Library/Application Support/TorBrowser-Data/Tor/control.socket"
                            )

                        self.c = Controller.from_socket_file(path=socket_file_path)
                        found_tor = True
                    except:
                        pass

            # If connecting to default control ports failed, so let's try
            # guessing the socket file name next
            if not found_tor:
                try:
                    if self.common.platform == "Linux" or self.common.platform == "BSD":
                        socket_file_path = (
                            f"/run/user/{os.geteuid()}/Tor/control.socket"
                        )
                    elif self.common.platform == "Darwin":
                        socket_file_path = (
                            f"/run/user/{os.geteuid()}/Tor/control.socket"
                        )
                    elif self.common.platform == "Windows":
                        # Windows doesn't support unix sockets
                        raise TorErrorAutomatic(strings._("settings_error_automatic"))

                    self.c = Controller.from_socket_file(path=socket_file_path)

                except:
                    raise TorErrorAutomatic(strings._("settings_error_automatic"))

            # Try authenticating
            try:
                self.c.authenticate()
            except:
                raise TorErrorAutomatic(strings._("settings_error_automatic"))

        else:
            # Use specific settings to connect to tor

            # Try connecting
            try:
                if self.settings.get("connection_type") == "control_port":
                    self.c = Controller.from_port(
                        address=self.settings.get("control_port_address"),
                        port=self.settings.get("control_port_port"),
                    )
                elif self.settings.get("connection_type") == "socket_file":
                    self.c = Controller.from_socket_file(
                        path=self.settings.get("socket_file_path")
                    )
                else:
                    raise TorErrorInvalidSetting(strings._("settings_error_unknown"))

            except:
                if self.settings.get("connection_type") == "control_port":
                    raise TorErrorSocketPort(
                        strings._("settings_error_socket_port").format(
                            self.settings.get("control_port_address"),
                            self.settings.get("control_port_port"),
                        )
                    )
                else:
                    raise TorErrorSocketFile(
                        strings._("settings_error_socket_file").format(
                            self.settings.get("socket_file_path")
                        )
                    )

            # Try authenticating
            try:
                if self.settings.get("auth_type") == "no_auth":
                    self.c.authenticate()
                elif self.settings.get("auth_type") == "password":
                    self.c.authenticate(self.settings.get("auth_password"))
                else:
                    raise TorErrorInvalidSetting(strings._("settings_error_unknown"))

            except MissingPassword:
                raise TorErrorMissingPassword(
                    strings._("settings_error_missing_password")
                )
            except UnreadableCookieFile:
                raise TorErrorUnreadableCookieFile(
                    strings._("settings_error_unreadable_cookie_file")
                )
            except AuthenticationFailure:
                raise TorErrorAuthError(
                    strings._("settings_error_auth").format(
                        self.settings.get("control_port_address"),
                        self.settings.get("control_port_port"),
                    )
                )

        # If we made it this far, we should be connected to Tor
        self.connected_to_tor = True

        # Get the tor version
        self.tor_version = self.c.get_version().version_str
        self.common.log("Onion", "connect", f"Connected to tor {self.tor_version}")

        # Do the versions of stem and tor that I'm using support ephemeral onion services?
        list_ephemeral_hidden_services = getattr(
            self.c, "list_ephemeral_hidden_services", None
        )
        self.supports_ephemeral = (
            callable(list_ephemeral_hidden_services) and self.tor_version >= "0.2.7.1"
        )

        # Do the versions of stem and tor that I'm using support stealth onion services?
        try:
            res = self.c.create_ephemeral_hidden_service(
                {1: 1},
                basic_auth={"onionshare": None},
                await_publication=False,
                key_type="NEW",
                key_content="RSA1024",
            )
            tmp_service_id = res.service_id
            self.c.remove_ephemeral_hidden_service(tmp_service_id)
            self.supports_stealth = True
        except:
            # ephemeral stealth onion services are not supported
            self.supports_stealth = False

        # Does this version of Tor support next-gen ('v3') onions?
        # Note, this is the version of Tor where this bug was fixed:
        # https://trac.torproject.org/projects/tor/ticket/28619
        self.supports_v3_onions = self.tor_version >= Version("0.3.5.7")

    def is_authenticated(self):
        """
        Returns True if the Tor connection is still working, or False otherwise.
        """
        if self.c is not None:
            return self.c.is_authenticated()
        else:
            return False

    def start_onion_service(self, mode_settings, port, await_publication):
        """
        Start a onion service on port 80, pointing to the given port, and
        return the onion hostname.
        """
        self.common.log("Onion", "start_onion_service", f"port={port}")

        if not self.supports_ephemeral:
            raise TorTooOld(strings._("error_ephemeral_not_supported"))
        if mode_settings.get("general", "client_auth") and not self.supports_stealth:
            raise TorTooOld(strings._("error_stealth_not_supported"))

        auth_cookie = None
        if mode_settings.get("general", "client_auth"):
            if mode_settings.get("onion", "hidservauth_string"):
                auth_cookie = mode_settings.get("onion", "hidservauth_string").split()[
                    2
                ]
            if auth_cookie:
                basic_auth = {"onionshare": auth_cookie}
            else:
                # If we had neither a scheduled auth cookie or a persistent hidservauth string,
                # set the cookie to 'None', which means Tor will create one for us
                basic_auth = {"onionshare": None}
        else:
            # Not using client auth at all
            basic_auth = None

        if mode_settings.get("onion", "private_key"):
            key_content = mode_settings.get("onion", "private_key")
            if self.is_v2_key(key_content):
                key_type = "RSA1024"
            else:
                # Assume it was a v3 key. Stem will throw an error if it's something illegible
                key_type = "ED25519-V3"
        else:
            key_type = "NEW"
            # Work out if we can support v3 onion services, which are preferred
            if self.supports_v3_onions and not mode_settings.get("general", "legacy"):
                key_content = "ED25519-V3"
            else:
                # fall back to v2 onion services
                key_content = "RSA1024"

        # v3 onions don't yet support basic auth. Our ticket:
        # https://github.com/micahflee/onionshare/issues/697
        if (
            key_type == "NEW"
            and key_content == "ED25519-V3"
            and not mode_settings.get("general", "legacy")
        ):
            basic_auth = None

        debug_message = f"key_type={key_type}"
        if key_type == "NEW":
            debug_message += f", key_content={key_content}"
        self.common.log("Onion", "start_onion_service", debug_message)
        try:
            res = self.c.create_ephemeral_hidden_service(
                {80: port},
                await_publication=await_publication,
                basic_auth=basic_auth,
                key_type=key_type,
                key_content=key_content,
            )

        except ProtocolError as e:
            raise TorErrorProtocolError(
                strings._("error_tor_protocol_error").format(e.args[0])
            )

        onion_host = res.service_id + ".onion"

        # Save the service_id
        mode_settings.set("general", "service_id", res.service_id)

        # Save the private key and hidservauth string
        if not mode_settings.get("onion", "private_key"):
            mode_settings.set("onion", "private_key", res.private_key)
        if mode_settings.get("general", "client_auth") and not mode_settings.get(
            "onion", "hidservauth_string"
        ):
            auth_cookie = list(res.client_auth.values())[0]
            self.auth_string = f"HidServAuth {onion_host} {auth_cookie}"
            mode_settings.set("onion", "hidservauth_string", self.auth_string)

        return onion_host

    def stop_onion_service(self, mode_settings):
        """
        Stop a specific onion service
        """
        onion_host = mode_settings.get("general", "service_id")
        if onion_host:
            self.common.log("Onion", "stop_onion_service", f"onion host: {onion_host}")
            try:
                self.c.remove_ephemeral_hidden_service(
                    mode_settings.get("general", "service_id")
                )
            except:
                self.common.log(
                    "Onion", "stop_onion_service", f"failed to remove {onion_host}"
                )

    def cleanup(self, stop_tor=True):
        """
        Stop onion services that were created earlier. If there's a tor subprocess running, kill it.
        """
        self.common.log("Onion", "cleanup")

        # Cleanup the ephemeral onion services, if we have any
        try:
            onions = self.c.list_ephemeral_hidden_services()
            for service_id in onions:
                onion_host = f"{service_id}.onion"
                try:
                    self.common.log(
                        "Onion", "cleanup", f"trying to remove onion {onion_host}"
                    )
                    self.c.remove_ephemeral_hidden_service(service_id)
                except:
                    self.common.log(
                        "Onion", "cleanup", f"failed to remove onion {onion_host}"
                    )
                    pass
        except:
            pass

        if stop_tor:
            # Stop tor process
            if self.tor_proc:
                self.tor_proc.terminate()
                time.sleep(0.2)
                if self.tor_proc.poll() is None:
                    self.common.log(
                        "Onion",
                        "cleanup",
                        "Tried to terminate tor process but it's still running",
                    )
                    try:
                        self.tor_proc.kill()
                        time.sleep(0.2)
                        if self.tor_proc.poll() is None:
                            self.common.log(
                                "Onion",
                                "cleanup",
                                "Tried to kill tor process but it's still running",
                            )
                    except:
                        self.common.log(
                            "Onion", "cleanup", "Exception while killing tor process"
                        )
                self.tor_proc = None

            # Reset other Onion settings
            self.connected_to_tor = False

            try:
                # Delete the temporary tor data directory
                if self.use_tmp_dir:
                    self.tor_data_directory.cleanup()
            except:
                pass

    def get_tor_socks_port(self):
        """
        Returns a (address, port) tuple for the Tor SOCKS port
        """
        self.common.log("Onion", "get_tor_socks_port")

        if self.settings.get("connection_type") == "bundled":
            return ("127.0.0.1", self.tor_socks_port)
        elif self.settings.get("connection_type") == "automatic":
            return ("127.0.0.1", 9150)
        else:
            return (self.settings.get("socks_address"), self.settings.get("socks_port"))

    def is_v2_key(self, key):
        """
        Helper function for determining if a key is RSA1024 (v2) or not.
        """
        try:
            # Import the key
            key = RSA.importKey(base64.b64decode(key))
            # Is this a v2 Onion key? (1024 bits) If so, we should keep using it.
            if key.n.bit_length() == 1024:
                return True
            else:
                return False
        except:
            return False