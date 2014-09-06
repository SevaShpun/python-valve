# -*- coding: utf-8 -*-
# Copyright (C) 2014 Oliver Ainsworth

from __future__ import (absolute_import,
                        unicode_literals, print_function, division)

import collections
import contextlib
import functools
import json
import string
import textwrap
import types
import warnings
import xml.etree.ElementTree as etree

import requests
import six

from ... import vdf


API_RESPONSE_FORMATS = {"json", "vdf", "xml"}


def api_response_format(format):
    if format not in API_RESPONSE_FORMATS:
        raise ValueError("Bad response format {!r}".format(format))

    def decorator(function):

        @functools.wraps(function)
        def wrapper(response):
            return function(response)

        wrapper.format = format
        return wrapper

    return decorator


@api_response_format("json")
def json_format(response):
    """Parse response as JSON using the standard Python JSON parser

    :return: the JSON object encoded in the response.
    """
    return json.loads(response)


@api_response_format("xml")
def etree_format(response):
    """Parse response using ElementTree

    :return: a :class:`xml.etree.ElementTree.Element` of the root element of
        the response.
    """
    return etree.fromstring(response)


@api_response_format("vdf")
def vdf_format(response):
    """Parse response using :mod:`valve.vdf`

    :return: a dictionary decoded from the VDF.
    """
    return vdf.loads(response)


def uint32(value):
    """Validate a 'unit32' method parameter type"""
    value = int(value)
    if value > 4294967295:
        raise ValueError("{} exceeds upper bound for uint32".format(value))
    if value < 0:
        raise ValueError("{} below lower bound for uint32".format(value))
    return value


def uint64(value):
    """Validate a 'unit64' method parameter type"""
    value = int(value)
    if value > 18446744073709551615:
        raise ValueError("{} exceeds upper bound for uint64".format(value))
    if value < 0:
        raise ValueError("{} below lower bound for uint64".format(value))
    return value


PARAMETER_TYPES = {
    "string": str,
    "uint32": uint32,
    "uint64": uint64,
}


class BaseInterface(object):

    def __init__(self, api):
        self._api = api

    def _request(self, http_method, method, version, params):
        return self._api.request(http_method,
                                 self.name, method, version, params)

    def __iter__(self):
        """An iterator of all interface methods

        Implemented by :func:`make_interface`.
        """
        raise NotImplementedError


def _ensure_identifier(name):
    """Convert ``name`` to a valid Python identifier

    Returns a valid Python identifier in the form ``[A-Za-z_][0-9A-Za-z_]*``.
    Any invalid characters are stripped away. Then all numeric leading
    characters are also stripped away.

    :raises NameError: if a valid identifier cannot be formed.
    """
    # Note: the identifiers generated by this function must be safe
    # for use with eval()
    identifier = "".join(char for char in name
                       if char in string.ascii_letters + string.digits + "_")
    try:
        while identifier[0] not in string.ascii_letters + "_":
            identifier = identifier[1:]
    except IndexError:
        raise NameError(
            "Cannot form valid Python identifier from {!r}".format(name))
    return identifier


class _MethodParameters(collections.OrderedDict):
    """Represents the parameters accepted by a Steam API interface method

    Parameters are sorted alphabetically by their name.
    """

    def __init__(self, specs):
        unordered = {}
        for spec in specs:
            if spec["name"] == "key":
                # This is applied in API.request()
                continue
            spec["name"] = _ensure_identifier(spec["name"])
            if spec["name"] in unordered:
                # Hopefully this will never happen ...
                raise NameError("Parameter name {!r} "
                                "already in use".format(spec["name"]))
            if "description" not in spec:
                spec["description"] = ""
            if spec["type"] not in PARAMETER_TYPES:
                warnings.warn(
                    "No parameter type handler for {!r}, interpreting "
                    "as 'string'; however this may change in "
                    "the future".format(spec["type"]), FutureWarning)
                spec["type"] = "string"
            unordered[spec["name"]] = spec
        super(_MethodParameters, self).__init__(
            sorted(unordered.items(), key=lambda a: a[0]))

    @property
    def signature(self):
        """Get the method signature as a string

        Firstly the the parameters are split into mandatory and optional groups.
        The mandatory fields with no default are always first so it's valid
        syntaxically. These are sorted alphabetically. The optional parameters
        follow with their default set to None. These are also sorted
        alphabetically.

        Includes the leading 'self' argument.
        """
        signature = ["self"]
        optional = []
        mandatory = []
        for param in self.values():
            if param["optional"]:
                optional.append(param)
            else:
                mandatory.append(param)
        signature.extend(param["name"] for param in mandatory)
        signature.extend(param["name"] + "=None" for param in optional)
        return ", ".join(signature)

    def validate(self, **kwargs):
        """Validate key-word arguments

        Validates and coerces arguments to the correct type when making the
        HTTP request. Optional parameters which are not given or are set to
        None are not included in the returned dictionary.

        :raises TypeError: if any mandatory arguments are missing.
        :return: a dictionary of parameters to be sent with the method request.
        """
        values = {}
        for arg in self.values():
            value = kwargs.get(arg["name"])
            if value is None:
                if not arg["optional"]:
                    # Technically the method signature protects against this
                    # ever happening
                    raise TypeError(
                        "Missing mandatory argument {!r}".format(arg["name"]))
                else:
                    continue
            values[arg["name"]] = PARAMETER_TYPES[arg["type"]](value)
        return values


def make_method(spec):
    """Make an interface method

    This takes a dictionary like that is returned by
    ISteamWebAPIUtil/GetSupportedAPIList (in JSON) which describes a method
    for an interface.

    The specification is expected to have the following keys:

        * ``name``
        * ``version``
        * ``httpmethod``
        * ``parameters``
    """
    spec["name"] = _ensure_identifier(spec["name"])
    args = _MethodParameters(spec["parameters"])

    def method(self, **kwargs):
        return self._request(spec["httpmethod"], spec["name"],
                             spec["version"], args.validate(**kwargs))

    # Do some eval() voodoo so we can rewrite the method signature. Otherwise
    # when something like autodoc sees it, it'll just output f(**kwargs) which
    # is really lame. _ensure_identifiers sanitises the function and
    # argument names it's safe.
    eval_globals = {}
    code = compile(
        textwrap.dedent("""
            def {}({}):
                return method(**locals())
            """.format(spec["name"], args.signature)),
        "<voodoo>",
        "exec",
    )
    eval(code, {"method": method}, eval_globals)
    method = eval_globals[spec["name"]]
    method.version = spec["version"]
    method.name = spec["name"]
    method.__name__ = spec["name"] if six.PY3 else bytes(spec["name"])
    param_docs = []
    for arg, param_spec in args.items():
        param_docs.append(
            ":param {type} {arg}: {description}".format(arg=arg, **param_spec))
    method.__doc__ = "\n".join(param_docs) if param_docs else None
    return method


def make_interface(spec, versions):
    """Build an interface class

    This takes an interface specification as returned by
    ``ISteamWebAPIUtil/GetSupportedAPIList`` and builds a :class:`BaseInterface`
    subclass from it.

    An interface specification may define methods which have multiple versions.
    If an entry for the method exists in ``versions`` then that version will be
    used. Otherwise the version of the method with the highest version will be.

    :param api_list: a JSON-decoded interface specification taken from a
        response to a ``ISteamWebAPIUtil/GetSupportedAPIList/v1`` request.
    :param versions: a dictionary of method versions to use for the interface.
    """
    methods = {}
    max_versions = {}
    attrs = {"name": spec["name"],
             "__iter__": lambda self: iter(methods.values())}
    for method_spec in spec["methods"]:
        method = make_method(method_spec)
        # Take care to use method.name as it's make_method that has ultimate
        # authority over the naming of a method.
        pinned_version = versions.get(method.name)
        if pinned_version is None:
            # Version not pinned, so just use the highest one
            current_method = methods.get(method.name)
            if (current_method is not None
                    and method.version < current_method.version):
                method = current_method
            methods[method.name] = method
        else:
            if method.version == pinned_version:
                methods[method.name] = method
        max_versions[method.name] = max(method.version,
                                        max_versions.get(method.name, 0))
    for method in methods.values():
        if method.version < max_versions[method.name]:
            warnings.warn(
                "{interface}/{meth.name} is pinned to version {meth.version}"
                " but the most recent version is {version}".format(
                    interface=spec["name"],
                    meth=method,
                    version=max_versions[method.name]
                ),
                FutureWarning,
            )
    attrs.update(methods)
    return type(
        spec["name"] if six.PY3 else bytes(spec["name"]),
        (BaseInterface,),
        attrs,
    )


def make_interfaces(api_list, versions):
    """Build a module of interface classes

    Takes a JSON response of ``ISteamWebAPIUtil/GetSupportedAPIList`` and
    builds a module of :class:`BaseInterface` subclasses for each listed
    interface.

    :param api_list: a JSON-decoded response to a
        ``ISteamWebAPIUtil/GetSupportedAPIList/v1`` request.
    :param versions: a dictionary of interface method versions.
    :return: a module of :class:`BaseInterface` subclasses.
    """
    module = types.ModuleType("interfaces" if six.PY3 else b"interfaces")
    module.__all__ = []
    for interface_spec in api_list["apilist"]["interfaces"]:
        interface = make_interface(interface_spec,
                                   versions.get(interface_spec["name"], {}))
        module.__all__.append(interface.__name__)
        setattr(module, interface.__name__, interface)
    return module


class API(object):

    api_root = "https://api.steampowered.com/"

    def __init__(self, key=None, format="json", versions=None, interfaces=None):
        """Initialise an API wrapper

        The API is usable without an API key but exposes significantly less
        functionality, therefore it's advisable to use a key.

        Response formatters are callables which take the Unicode response from
        the Steam Web API and turn it into a more usable Python object, such as
        dictionary. The Steam API it self can generate responses in either
        JSON, XML or VDF. The formatter callables should have an attribute
        ``format`` which is a string indicating which textual format they
        handle. For convenience the ``format`` parameter also accepts the
        strings ``json``, ``xml`` and ``vdf`` which are mapped to the
        :func:`json_format`, :func:`etree_format` and :func:`vdf_format`
        formatters respectively.

        The ``interfaces`` argument can optionally be set to a module
        containing :class:`BaseInterface` subclasses which will be instantiated
        and bound to the :class:`API` instance. If not given then the
        interfaces are loaded using ``ISteamWebAPIUtil/GetSupportedAPIList``.

        The optional ``versions`` argument allows specific versions of interface
        methods to be used. If given, ``versions`` should be a mapping of
        further mappings keyed against the interface name. The inner mapping
        should specify the version of interface method to use which is keyed
        against the method name. These mappings don't need to be complete and
        can omit methods or even entire interfaces. In which case the default
        behaviour is to use the method with the highest version number.

        :param str key: a Steam Web API key.
        :param format: response formatter.
        :param versions: the interface method versions to use.
        :param interfaces: a module containing :class:`BaseInterface`
            subclasses or ``None`` if they should be loaded for the first time.
        """
        self.key = key
        if format == "json":
            format = json_format
        elif format == "xml":
            format = etree_format
        elif format == "vdf":
            format = vdf_format
        self.format = format
        self._session = requests.Session()
        if interfaces is None:
            self._interfaces_module = make_interfaces(
                self.request("GET", "ISteamWebAPIUtil",
                             "GetSupportedAPIList", 1, format=json_format),
                versions or {},
            )
        else:
            self._interfaces_module = interfaces
        self._bind_interfaces()

    def __getitem__(self, interface_name):
        """Get an interface instance by name"""
        return self._interfaces[interface_name]

    def _bind_interfaces(self):
        """Bind all interfaces to this API instance

        Instantiate all :class:`BaseInterface` subclasses in the
        :attr:`_interfaces_module` with a reference to this :class:`API`
        instance.

        Sets :attr:`_interfaces` to a dictionary mapping interface names to
        corresponding instances.
        """
        self._interfaces = {}
        for name, interface in self._interfaces_module.__dict__.items():
            try:
                if issubclass(interface, BaseInterface):
                    self._interfaces[name] = interface(self)
            except TypeError:
                # Not a class
                continue

    def request(self, http_method, interface,
                method, version, params=None, format=None):
        """Issue a HTTP request to the Steam Web API

        This is called indirectly by interface methods and should rarely be
        called directly. The response to the request is passed through the
        response formatter which is then returned.

        :param str interface: the name of the interface.
        :param str method: the name of the method on the interface.
        :param int version: the version of the method.
        :param params: a mapping of GET or POST data to be sent with the
            request.
        :param format: a response formatter callable to overide :attr:`format`.
        """
        if params is None:
            params = {}
        if format is None:
            format = self.format
        path = "{interface}/{method}/v{version}/".format(**locals())
        if format.format not in API_RESPONSE_FORMATS:
            raise ValueError("Response formatter specifies its format as "
                             "{!r}, but only 'json', 'xml' and 'vdf' "
                             "are permitted values".format(format.format))
        params["format"] = format.format
        if "key" in params:
            del params["key"]
        if self.key:
            params["key"] = self.key
        return format(self._session.request(http_method,
                                            self.api_root + path, params).text)

    @contextlib.contextmanager
    def session(self):
        """Create an API sub-session without rebuilding the interfaces

        This returns a context manager which yields a new :class:`API` instance
        with the same interfaces as the current one. The difference between
        this and creating a new :class:`API` manually is that this will avoid
        rebuilding the all interface classes which can be slow.
        """
        yield API(self.key, self.format, self._interfaces_module)

    def __iter__(self):
        """An iterator of all bound API interfaces"""
        for interface in self._interfaces.values():
            yield interface

    def pin_versions(self):
        """Get the versions of the methods for each interface

        This returns a dictionary of dictionaries which is keyed against
        interface names. The inner dictionaries map method names to method
        version numbers. This structure is suitable for passing in as the
        ``versions`` argument to :meth:`__init__`.
        """
        versions = {}
        for interface in self:
            method_versions = {}
            for method in interface:
                method_versions[method.name] = method.version
            versions[interface.name] = method_versions
        return versions
