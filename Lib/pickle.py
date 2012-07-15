"""Create portable serialized representations of Python objects.

See module copyreg for a mechanism for registering custom picklers.
See module pickletools source for extensive comments.

Classes:

    Pickler
    Unpickler

Functions:

    dump(object, file)
    dumps(object) -> string
    load(file) -> object
    loads(string) -> object

Misc variables:

    __version__
    format_version
    compatible_formats

"""

__version__ = "$Revision$"       # Code version

from types import FunctionType, BuiltinFunctionType
from copyreg import dispatch_table
from copyreg import _extension_registry, _inverted_registry, _extension_cache
import marshal
import sys
import struct
import re
import io
import codecs
import _compat_pickle

__all__ = ["PickleError", "PicklingError", "UnpicklingError", "Pickler",
           "Unpickler", "dump", "dumps", "load", "loads"]

# Shortcut for use in isinstance testing
bytes_types = (bytes, bytearray)

# These are purely informational; no code uses these.
format_version = "3.0"                  # File format version we write
compatible_formats = ["1.0",            # Original protocol 0
                      "1.1",            # Protocol 0 with INST added
                      "1.2",            # Original protocol 1
                      "1.3",            # Protocol 1 with BINFLOAT added
                      "2.0",            # Protocol 2
                      "3.0",            # Protocol 3
                      ]                 # Old format versions we can read

# This is the highest protocol number we know how to read.
HIGHEST_PROTOCOL = 3

# The protocol we write by default.  May be less than HIGHEST_PROTOCOL.
# We intentionally write a protocol that Python 2.x cannot read;
# there are too many issues with that.
DEFAULT_PROTOCOL = 3

# Why use struct.pack() for pickling but marshal.loads() for
# unpickling?  struct.pack() is 40% faster than marshal.dumps(), but
# marshal.loads() is twice as fast as struct.unpack()!
mloads = marshal.loads

class PickleError(Exception):
    """A common base class for the other pickling exceptions."""
    pass

class PicklingError(PickleError):
    """This exception is raised when an unpicklable object is passed to the
    dump() method.

    """
    pass

class UnpicklingError(PickleError):
    """This exception is raised when there is a problem unpickling an object,
    such as a security violation.

    Note that other exceptions may also be raised during unpickling, including
    (but not necessarily limited to) AttributeError, EOFError, ImportError,
    and IndexError.

    """
    pass

# An instance of _Stop is raised by Unpickler.load_stop() in response to
# the STOP opcode, passing the object that is the result of unpickling.
class _Stop(Exception):
    def __init__(self, value):
        self.value = value

# Jython has PyStringMap; it's a dict subclass with string keys
try:
    from org.python.core import PyStringMap
except ImportError:
    PyStringMap = None

# Pickle opcodes.  See pickletools.py for extensive docs.  The listing
# here is in kind-of alphabetical order of 1-character pickle code.
# pickletools groups them by purpose.

MARK           = b'('   # push special markobject on stack
STOP           = b'.'   # every pickle ends with STOP
POP            = b'0'   # discard topmost stack item
POP_MARK       = b'1'   # discard stack top through topmost markobject
DUP            = b'2'   # duplicate top stack item
FLOAT          = b'F'   # push float object; decimal string argument
INT            = b'I'   # push integer or bool; decimal string argument
BININT         = b'J'   # push four-byte signed int
BININT1        = b'K'   # push 1-byte unsigned int
LONG           = b'L'   # push long; decimal string argument
BININT2        = b'M'   # push 2-byte unsigned int
NONE           = b'N'   # push None
PERSID         = b'P'   # push persistent object; id is taken from string arg
BINPERSID      = b'Q'   #  "       "         "  ;  "  "   "     "  stack
REDUCE         = b'R'   # apply callable to argtuple, both on stack
STRING         = b'S'   # push string; NL-terminated string argument
BINSTRING      = b'T'   # push string; counted binary string argument
SHORT_BINSTRING= b'U'   #  "     "   ;    "      "       "      " < 256 bytes
UNICODE        = b'V'   # push Unicode string; raw-unicode-escaped'd argument
BINUNICODE     = b'X'   #   "     "       "  ; counted UTF-8 string argument
APPEND         = b'a'   # append stack top to list below it
BUILD          = b'b'   # call __setstate__ or __dict__.update()
GLOBAL         = b'c'   # push self.find_class(modname, name); 2 string args
DICT           = b'd'   # build a dict from stack items
EMPTY_DICT     = b'}'   # push empty dict
APPENDS        = b'e'   # extend list on stack by topmost stack slice
GET            = b'g'   # push item from memo on stack; index is string arg
BINGET         = b'h'   #   "    "    "    "   "   "  ;   "    " 1-byte arg
INST           = b'i'   # build & push class instance
LONG_BINGET    = b'j'   # push item from memo on stack; index is 4-byte arg
LIST           = b'l'   # build list from topmost stack items
EMPTY_LIST     = b']'   # push empty list
OBJ            = b'o'   # build & push class instance
PUT            = b'p'   # store stack top in memo; index is string arg
BINPUT         = b'q'   #   "     "    "   "   " ;   "    " 1-byte arg
LONG_BINPUT    = b'r'   #   "     "    "   "   " ;   "    " 4-byte arg
SETITEM        = b's'   # add key+value pair to dict
TUPLE          = b't'   # build tuple from topmost stack items
EMPTY_TUPLE    = b')'   # push empty tuple
SETITEMS       = b'u'   # modify dict by adding topmost key+value pairs
BINFLOAT       = b'G'   # push float; arg is 8-byte float encoding

TRUE           = b'I01\n'  # not an opcode; see INT docs in pickletools.py
FALSE          = b'I00\n'  # not an opcode; see INT docs in pickletools.py

# Protocol 2

PROTO          = b'\x80'  # identify pickle protocol
NEWOBJ         = b'\x81'  # build object by applying cls.__new__ to argtuple
EXT1           = b'\x82'  # push object from extension registry; 1-byte index
EXT2           = b'\x83'  # ditto, but 2-byte index
EXT4           = b'\x84'  # ditto, but 4-byte index
TUPLE1         = b'\x85'  # build 1-tuple from stack top
TUPLE2         = b'\x86'  # build 2-tuple from two topmost stack items
TUPLE3         = b'\x87'  # build 3-tuple from three topmost stack items
NEWTRUE        = b'\x88'  # push True
NEWFALSE       = b'\x89'  # push False
LONG1          = b'\x8a'  # push long from < 256 bytes
LONG4          = b'\x8b'  # push really big long

_tuplesize2code = [EMPTY_TUPLE, TUPLE1, TUPLE2, TUPLE3]

# Protocol 3 (Python 3.x)

BINBYTES       = b'B'   # push bytes; counted binary string argument
SHORT_BINBYTES = b'C'   #  "     "   ;    "      "       "      " < 256 bytes

__all__.extend([x for x in dir() if re.match("[A-Z][A-Z0-9_]+$",x)])

# Pickling machinery

class _Pickler:

    def __init__(self, file, protocol=None, *, fix_imports=True):
        """This takes a binary file for writing a pickle data stream.

        The optional protocol argument tells the pickler to use the
        given protocol; supported protocols are 0, 1, 2, 3.  The default
        protocol is 3; a backward-incompatible protocol designed for
        Python 3.0.

        Specifying a negative protocol version selects the highest
        protocol version supported.  The higher the protocol used, the
        more recent the version of Python needed to read the pickle
        produced.

        The file argument must have a write() method that accepts a single
        bytes argument. It can thus be a file object opened for binary
        writing, a io.BytesIO instance, or any other custom object that
        meets this interface.

        If fix_imports is True and protocol is less than 3, pickle will try to
        map the new Python 3.x names to the old module names used in Python
        2.x, so that the pickle data stream is readable with Python 2.x.
        """
        if protocol is None:
            protocol = DEFAULT_PROTOCOL
        if protocol < 0:
            protocol = HIGHEST_PROTOCOL
        elif not 0 <= protocol <= HIGHEST_PROTOCOL:
            raise ValueError("pickle protocol must be <= %d" % HIGHEST_PROTOCOL)
        try:
            self.write = file.write
        except AttributeError:
            raise TypeError("file must have a 'write' attribute")
        self.memo = {}
        self.proto = int(protocol)
        self.bin = protocol >= 1
        self.fast = 0
        self.fix_imports = fix_imports and protocol < 3
        ## Stackless addition BEGIN
        try:
            from stackless import _pickle_moduledict
        except ImportError:
            _pickle_moduledict = lambda self, obj:None
        self._pickle_moduledict = _pickle_moduledict
        ## Stackless addition END

    def clear_memo(self):
        """Clears the pickler's "memo".

        The memo is the data structure that remembers which objects the
        pickler has already seen, so that shared or recursive objects are
        pickled by reference and not by value.  This method is useful when
        re-using picklers.

        """
        self.memo.clear()

    def dump(self, obj):
        """Write a pickled representation of obj to the open file."""
        # Check whether Pickler was initialized correctly. This is
        # only needed to mimic the behavior of _pickle.Pickler.dump().
        if not hasattr(self, "write"):
            raise PicklingError("Pickler.__init__() was not called by "
                                "%s.__init__()" % (self.__class__.__name__,))
        if self.proto >= 2:
            self.write(PROTO + bytes([self.proto]))
        self.save(obj)
        self.write(STOP)

    def memoize(self, obj):
        """Store an object in the memo."""

        # The Pickler memo is a dictionary mapping object ids to 2-tuples
        # that contain the Unpickler memo key and the object being memoized.
        # The memo key is written to the pickle and will become
        # the key in the Unpickler's memo.  The object is stored in the
        # Pickler memo so that transient objects are kept alive during
        # pickling.

        # The use of the Unpickler memo length as the memo key is just a
        # convention.  The only requirement is that the memo values be unique.
        # But there appears no advantage to any other scheme, and this
        # scheme allows the Unpickler memo to be implemented as a plain (but
        # growable) array, indexed by memo key.
        if self.fast:
            return
        assert id(obj) not in self.memo
        memo_len = len(self.memo)
        self.write(self.put(memo_len))
        self.memo[id(obj)] = memo_len, obj

    # Return a PUT (BINPUT, LONG_BINPUT) opcode string, with argument i.
    def put(self, i, pack=struct.pack):
        if self.bin:
            if i < 256:
                return BINPUT + bytes([i])
            else:
                return LONG_BINPUT + pack("<i", i)

        return PUT + repr(i).encode("ascii") + b'\n'

    # Return a GET (BINGET, LONG_BINGET) opcode string, with argument i.
    def get(self, i, pack=struct.pack):
        if self.bin:
            if i < 256:
                return BINGET + bytes([i])
            else:
                return LONG_BINGET + pack("<i", i)

        return GET + repr(i).encode("ascii") + b'\n'

    def save(self, obj, save_persistent_id=True):
        # Check for persistent id (defined by a subclass)
        pid = self.persistent_id(obj)
        if pid is not None and save_persistent_id:
            self.save_pers(pid)
            return

        # Check the memo
        x = self.memo.get(id(obj))
        if x:
            self.write(self.get(x[0]))
            return

        # Check the type dispatch table
        t = type(obj)
        f = self.dispatch.get(t)
        if f:
            f(self, obj) # Call unbound method with explicit self
            return

        # Check copyreg.dispatch_table
        reduce = dispatch_table.get(t)
        if reduce:
            rv = reduce(obj)
        else:
            # Check for a class with a custom metaclass; treat as regular class
            try:
                issc = issubclass(t, type)
            except TypeError: # t is not a class (old Boost; see SF #502085)
                issc = False
            if issc:
                self.save_global(obj)
                return

            # Check for a __reduce_ex__ method, fall back to __reduce__
            reduce = getattr(obj, "__reduce_ex__", None)
            if reduce:
                rv = reduce(self.proto)
            else:
                reduce = getattr(obj, "__reduce__", None)
                if reduce:
                    rv = reduce()
                else:
                    raise PicklingError("Can't pickle %r object: %r" %
                                        (t.__name__, obj))

        # Check for string returned by reduce(), meaning "save as global"
        if isinstance(rv, str):
            self.save_global(obj, rv)
            return

        # Assert that reduce() returned a tuple
        if not isinstance(rv, tuple):
            raise PicklingError("%s must return string or tuple" % reduce)

        # Assert that it returned an appropriately sized tuple
        l = len(rv)
        if not (2 <= l <= 5):
            raise PicklingError("Tuple returned by %s must have "
                                "two to five elements" % reduce)

        # Save the reduce() output and finally memoize the object
        self.save_reduce(obj=obj, *rv)

    def persistent_id(self, obj):
        # This exists so a subclass can override it
        return None

    def save_pers(self, pid):
        # Save a persistent id reference
        if self.bin:
            self.save(pid, save_persistent_id=False)
            self.write(BINPERSID)
        else:
            self.write(PERSID + str(pid).encode("ascii") + b'\n')

    def save_reduce(self, func, args, state=None,
                    listitems=None, dictitems=None, obj=None):
        # This API is called by some subclasses

        # Assert that args is a tuple
        if not isinstance(args, tuple):
            raise PicklingError("args from save_reduce() should be a tuple")

        # Assert that func is callable
        if not callable(func):
            raise PicklingError("func from save_reduce() should be callable")

        save = self.save
        write = self.write

        # Protocol 2 special case: if func's name is __newobj__, use NEWOBJ
        if self.proto >= 2 and getattr(func, "__name__", "") == "__newobj__":
            # A __reduce__ implementation can direct protocol 2 to
            # use the more efficient NEWOBJ opcode, while still
            # allowing protocol 0 and 1 to work normally.  For this to
            # work, the function returned by __reduce__ should be
            # called __newobj__, and its first argument should be a
            # new-style class.  The implementation for __newobj__
            # should be as follows, although pickle has no way to
            # verify this:
            #
            # def __newobj__(cls, *args):
            #     return cls.__new__(cls, *args)
            #
            # Protocols 0 and 1 will pickle a reference to __newobj__,
            # while protocol 2 (and above) will pickle a reference to
            # cls, the remaining args tuple, and the NEWOBJ code,
            # which calls cls.__new__(cls, *args) at unpickling time
            # (see load_newobj below).  If __reduce__ returns a
            # three-tuple, the state from the third tuple item will be
            # pickled regardless of the protocol, calling __setstate__
            # at unpickling time (see load_build below).
            #
            # Note that no standard __newobj__ implementation exists;
            # you have to provide your own.  This is to enforce
            # compatibility with Python 2.2 (pickles written using
            # protocol 0 or 1 in Python 2.3 should be unpicklable by
            # Python 2.2).
            cls = args[0]
            if not hasattr(cls, "__new__"):
                raise PicklingError(
                    "args[0] from __newobj__ args has no __new__")
            if obj is not None and cls is not obj.__class__:
                raise PicklingError(
                    "args[0] from __newobj__ args has the wrong class")
            args = args[1:]
            save(cls)
            save(args)
            write(NEWOBJ)
        else:
            save(func)
            save(args)
            write(REDUCE)

        if obj is not None:
            self.memoize(obj)

        # More new special cases (that work with older protocols as
        # well): when __reduce__ returns a tuple with 4 or 5 items,
        # the 4th and 5th item should be iterators that provide list
        # items and dict items (as (key, value) tuples), or None.

        if listitems is not None:
            self._batch_appends(listitems)

        if dictitems is not None:
            self._batch_setitems(dictitems)

        if state is not None:
            save(state)
            write(BUILD)

    # Methods below this point are dispatched through the dispatch table

    dispatch = {}

    def save_none(self, obj):
        self.write(NONE)
    dispatch[type(None)] = save_none

    def save_bool(self, obj):
        if self.proto >= 2:
            self.write(obj and NEWTRUE or NEWFALSE)
        else:
            self.write(obj and TRUE or FALSE)
    dispatch[bool] = save_bool

    def save_long(self, obj, pack=struct.pack):
        if self.bin:
            # If the int is small enough to fit in a signed 4-byte 2's-comp
            # format, we can store it more efficiently than the general
            # case.
            # First one- and two-byte unsigned ints:
            if obj >= 0:
                if obj <= 0xff:
                    self.write(BININT1 + bytes([obj]))
                    return
                if obj <= 0xffff:
                    self.write(BININT2 + bytes([obj&0xff, obj>>8]))
                    return
            # Next check for 4-byte signed ints:
            high_bits = obj >> 31  # note that Python shift sign-extends
            if high_bits == 0 or high_bits == -1:
                # All high bits are copies of bit 2**31, so the value
                # fits in a 4-byte signed int.
                self.write(BININT + pack("<i", obj))
                return
        if self.proto >= 2:
            encoded = encode_long(obj)
            n = len(encoded)
            if n < 256:
                self.write(LONG1 + bytes([n]) + encoded)
            else:
                self.write(LONG4 + pack("<i", n) + encoded)
            return
        self.write(LONG + repr(obj).encode("ascii") + b'L\n')
    dispatch[int] = save_long

    def save_float(self, obj, pack=struct.pack):
        if self.bin:
            self.write(BINFLOAT + pack('>d', obj))
        else:
            self.write(FLOAT + repr(obj).encode("ascii") + b'\n')
    dispatch[float] = save_float

    def save_bytes(self, obj, pack=struct.pack):
        if self.proto < 3:
            if len(obj) == 0:
                self.save_reduce(bytes, (), obj=obj)
            else:
                self.save_reduce(codecs.encode,
                                 (str(obj, 'latin1'), 'latin1'), obj=obj)
            return
        n = len(obj)
        if n < 256:
            self.write(SHORT_BINBYTES + bytes([n]) + bytes(obj))
        else:
            self.write(BINBYTES + pack("<i", n) + bytes(obj))
        self.memoize(obj)
    dispatch[bytes] = save_bytes

    def save_str(self, obj, pack=struct.pack):
        if self.bin:
            encoded = obj.encode('utf-8', 'surrogatepass')
            n = len(encoded)
            self.write(BINUNICODE + pack("<i", n) + encoded)
        else:
            obj = obj.replace("\\", "\\u005c")
            obj = obj.replace("\n", "\\u000a")
            self.write(UNICODE + bytes(obj.encode('raw-unicode-escape')) +
                       b'\n')
        self.memoize(obj)
    dispatch[str] = save_str

    def save_tuple(self, obj):
        write = self.write
        proto = self.proto

        n = len(obj)
        if n == 0:
            if proto:
                write(EMPTY_TUPLE)
            else:
                write(MARK + TUPLE)
            return

        save = self.save
        memo = self.memo
        if n <= 3 and proto >= 2:
            for element in obj:
                save(element)
            # Subtle.  Same as in the big comment below.
            if id(obj) in memo:
                get = self.get(memo[id(obj)][0])
                write(POP * n + get)
            else:
                write(_tuplesize2code[n])
                self.memoize(obj)
            return

        # proto 0 or proto 1 and tuple isn't empty, or proto > 1 and tuple
        # has more than 3 elements.
        write(MARK)
        for element in obj:
            save(element)

        if id(obj) in memo:
            # Subtle.  d was not in memo when we entered save_tuple(), so
            # the process of saving the tuple's elements must have saved
            # the tuple itself:  the tuple is recursive.  The proper action
            # now is to throw away everything we put on the stack, and
            # simply GET the tuple (it's already constructed).  This check
            # could have been done in the "for element" loop instead, but
            # recursive tuples are a rare thing.
            get = self.get(memo[id(obj)][0])
            if proto:
                write(POP_MARK + get)
            else:   # proto 0 -- POP_MARK not available
                write(POP * (n+1) + get)
            return

        # No recursion.
        self.write(TUPLE)
        self.memoize(obj)

    dispatch[tuple] = save_tuple

    def save_list(self, obj):
        write = self.write

        if self.bin:
            write(EMPTY_LIST)
        else:   # proto 0 -- can't use EMPTY_LIST
            write(MARK + LIST)

        self.memoize(obj)
        self._batch_appends(obj)

    dispatch[list] = save_list

    _BATCHSIZE = 1000

    def _batch_appends(self, items):
        # Helper to batch up APPENDS sequences
        save = self.save
        write = self.write

        if not self.bin:
            for x in items:
                save(x)
                write(APPEND)
            return

        items = iter(items)
        r = range(self._BATCHSIZE)
        while items is not None:
            tmp = []
            for i in r:
                try:
                    x = next(items)
                    tmp.append(x)
                except StopIteration:
                    items = None
                    break
            n = len(tmp)
            if n > 1:
                write(MARK)
                for x in tmp:
                    save(x)
                write(APPENDS)
            elif n:
                save(tmp[0])
                write(APPEND)
            # else tmp is empty, and we're done

    def save_dict(self, obj):
        ## Stackless addition BEGIN
        modict_saver = self._pickle_moduledict(self, obj)
        if modict_saver is not None:
            return self.save_reduce(*modict_saver)
        ## Stackless addition END
        write = self.write

        if self.bin:
            write(EMPTY_DICT)
        else:   # proto 0 -- can't use EMPTY_DICT
            write(MARK + DICT)

        self.memoize(obj)
        self._batch_setitems(obj.items())

    dispatch[dict] = save_dict
    if PyStringMap is not None:
        dispatch[PyStringMap] = save_dict

    def _batch_setitems(self, items):
        # Helper to batch up SETITEMS sequences; proto >= 1 only
        save = self.save
        write = self.write

        if not self.bin:
            for k, v in items:
                save(k)
                save(v)
                write(SETITEM)
            return

        items = iter(items)
        r = range(self._BATCHSIZE)
        while items is not None:
            tmp = []
            for i in r:
                try:
                    tmp.append(next(items))
                except StopIteration:
                    items = None
                    break
            n = len(tmp)
            if n > 1:
                write(MARK)
                for k, v in tmp:
                    save(k)
                    save(v)
                write(SETITEMS)
            elif n:
                k, v = tmp[0]
                save(k)
                save(v)
                write(SETITEM)
            # else tmp is empty, and we're done

    def save_global(self, obj, name=None, pack=struct.pack):
        write = self.write
        memo = self.memo

        if name is None:
            name = obj.__name__

        module = getattr(obj, "__module__", None)
        if module is None:
            module = whichmodule(obj, name)

        try:
            __import__(module, level=0)
            mod = sys.modules[module]
            klass = getattr(mod, name)
        except (ImportError, KeyError, AttributeError):
            raise PicklingError(
                "Can't pickle %r: it's not found as %s.%s" %
                (obj, module, name))
        else:
            if klass is not obj:
                raise PicklingError(
                    "Can't pickle %r: it's not the same object as %s.%s" %
                    (obj, module, name))

        if self.proto >= 2:
            code = _extension_registry.get((module, name))
            if code:
                assert code > 0
                if code <= 0xff:
                    write(EXT1 + bytes([code]))
                elif code <= 0xffff:
                    write(EXT2 + bytes([code&0xff, code>>8]))
                else:
                    write(EXT4 + pack("<i", code))
                return
        # Non-ASCII identifiers are supported only with protocols >= 3.
        if self.proto >= 3:
            write(GLOBAL + bytes(module, "utf-8") + b'\n' +
                  bytes(name, "utf-8") + b'\n')
        else:
            if self.fix_imports:
                if (module, name) in _compat_pickle.REVERSE_NAME_MAPPING:
                    module, name = _compat_pickle.REVERSE_NAME_MAPPING[(module, name)]
                if module in _compat_pickle.REVERSE_IMPORT_MAPPING:
                    module = _compat_pickle.REVERSE_IMPORT_MAPPING[module]
            try:
                write(GLOBAL + bytes(module, "ascii") + b'\n' +
                      bytes(name, "ascii") + b'\n')
            except UnicodeEncodeError:
                raise PicklingError(
                    "can't pickle global identifier '%s.%s' using "
                    "pickle protocol %i" % (module, name, self.proto))

        self.memoize(obj)

    def save_function(self, obj):
        try:
            return self.save_global(obj)
        except PicklingError as e:
            pass
        # Check copy_reg.dispatch_table
        reduce = dispatch_table.get(type(obj))
        if reduce:
            rv = reduce(obj)
        else:
            # Check for a __reduce_ex__ method, fall back to __reduce__
            reduce = getattr(obj, "__reduce_ex__", None)
            if reduce:
                rv = reduce(self.proto)
            else:
                reduce = getattr(obj, "__reduce__", None)
                if reduce:
                    rv = reduce()
                else:
                    raise e
        return self.save_reduce(obj=obj, *rv)

    dispatch[FunctionType] = save_function
    dispatch[BuiltinFunctionType] = save_global
    dispatch[type] = save_global

# Pickling helpers

def _keep_alive(x, memo):
    """Keeps a reference to the object x in the memo.

    Because we remember objects by their id, we have
    to assure that possibly temporary objects are kept
    alive by referencing them.
    We store a reference at the id of the memo, which should
    normally not be used unless someone tries to deepcopy
    the memo itself...
    """
    try:
        memo[id(memo)].append(x)
    except KeyError:
        # aha, this is the first one :-)
        memo[id(memo)]=[x]


# A cache for whichmodule(), mapping a function object to the name of
# the module in which the function was found.

classmap = {} # called classmap for backwards compatibility

# This is no longer used to find classes, but still for functions
def whichmodule(func, funcname):
    """Figure out the module in which a function occurs.

    Search sys.modules for the module.
    Cache in classmap.
    Return a module name.
    If the function cannot be found, return "__main__".
    """
    # Python functions should always get an __module__ from their globals.
    mod = getattr(func, "__module__", None)
    if mod is not None:
        return mod
    if func in classmap:
        return classmap[func]

    for name, module in list(sys.modules.items()):
        if module is None:
            continue # skip dummy package entries
        if name != '__main__' and getattr(module, funcname, None) is func:
            break
    else:
        name = '__main__'
    classmap[func] = name
    return name


# Unpickling machinery

class _Unpickler:

    def __init__(self, file, *, fix_imports=True,
                 encoding="ASCII", errors="strict"):
        """This takes a binary file for reading a pickle data stream.

        The protocol version of the pickle is detected automatically, so no
        proto argument is needed.

        The file-like object must have two methods, a read() method
        that takes an integer argument, and a readline() method that
        requires no arguments.  Both methods should return bytes.
        Thus file-like object can be a binary file object opened for
        reading, a BytesIO object, or any other custom object that
        meets this interface.

        Optional keyword arguments are *fix_imports*, *encoding* and *errors*,
        which are used to control compatiblity support for pickle stream
        generated by Python 2.x.  If *fix_imports* is True, pickle will try to
        map the old Python 2.x names to the new names used in Python 3.x.  The
        *encoding* and *errors* tell pickle how to decode 8-bit string
        instances pickled by Python 2.x; these default to 'ASCII' and
        'strict', respectively.
        """
        self.readline = file.readline
        self.read = file.read
        self.memo = {}
        self.encoding = encoding
        self.errors = errors
        self.proto = 0
        self.fix_imports = fix_imports

    def load(self):
        """Read a pickled object representation from the open file.

        Return the reconstituted object hierarchy specified in the file.
        """
        # Check whether Unpickler was initialized correctly. This is
        # only needed to mimic the behavior of _pickle.Unpickler.dump().
        if not hasattr(self, "read"):
            raise UnpicklingError("Unpickler.__init__() was not called by "
                                  "%s.__init__()" % (self.__class__.__name__,))
        self.mark = object() # any new unique object
        self.stack = []
        self.append = self.stack.append
        read = self.read
        dispatch = self.dispatch
        try:
            while 1:
                key = read(1)
                if not key:
                    raise EOFError
                assert isinstance(key, bytes_types)
                dispatch[key[0]](self)
        except _Stop as stopinst:
            return stopinst.value

    # Return largest index k such that self.stack[k] is self.mark.
    # If the stack doesn't contain a mark, eventually raises IndexError.
    # This could be sped by maintaining another stack, of indices at which
    # the mark appears.  For that matter, the latter stack would suffice,
    # and we wouldn't need to push mark objects on self.stack at all.
    # Doing so is probably a good thing, though, since if the pickle is
    # corrupt (or hostile) we may get a clue from finding self.mark embedded
    # in unpickled objects.
    def marker(self):
        stack = self.stack
        mark = self.mark
        k = len(stack)-1
        while stack[k] is not mark: k = k-1
        return k

    def persistent_load(self, pid):
        raise UnpicklingError("unsupported persistent id encountered")

    dispatch = {}

    def load_proto(self):
        proto = ord(self.read(1))
        if not 0 <= proto <= HIGHEST_PROTOCOL:
            raise ValueError("unsupported pickle protocol: %d" % proto)
        self.proto = proto
    dispatch[PROTO[0]] = load_proto

    def load_persid(self):
        pid = self.readline()[:-1].decode("ascii")
        self.append(self.persistent_load(pid))
    dispatch[PERSID[0]] = load_persid

    def load_binpersid(self):
        pid = self.stack.pop()
        self.append(self.persistent_load(pid))
    dispatch[BINPERSID[0]] = load_binpersid

    def load_none(self):
        self.append(None)
    dispatch[NONE[0]] = load_none

    def load_false(self):
        self.append(False)
    dispatch[NEWFALSE[0]] = load_false

    def load_true(self):
        self.append(True)
    dispatch[NEWTRUE[0]] = load_true

    def load_int(self):
        data = self.readline()
        if data == FALSE[1:]:
            val = False
        elif data == TRUE[1:]:
            val = True
        else:
            try:
                val = int(data, 0)
            except ValueError:
                val = int(data, 0)
        self.append(val)
    dispatch[INT[0]] = load_int

    def load_binint(self):
        self.append(mloads(b'i' + self.read(4)))
    dispatch[BININT[0]] = load_binint

    def load_binint1(self):
        self.append(ord(self.read(1)))
    dispatch[BININT1[0]] = load_binint1

    def load_binint2(self):
        self.append(mloads(b'i' + self.read(2) + b'\000\000'))
    dispatch[BININT2[0]] = load_binint2

    def load_long(self):
        val = self.readline()[:-1].decode("ascii")
        if val and val[-1] == 'L':
            val = val[:-1]
        self.append(int(val, 0))
    dispatch[LONG[0]] = load_long

    def load_long1(self):
        n = ord(self.read(1))
        data = self.read(n)
        self.append(decode_long(data))
    dispatch[LONG1[0]] = load_long1

    def load_long4(self):
        n = mloads(b'i' + self.read(4))
        data = self.read(n)
        self.append(decode_long(data))
    dispatch[LONG4[0]] = load_long4

    def load_float(self):
        self.append(float(self.readline()[:-1]))
    dispatch[FLOAT[0]] = load_float

    def load_binfloat(self, unpack=struct.unpack):
        self.append(unpack('>d', self.read(8))[0])
    dispatch[BINFLOAT[0]] = load_binfloat

    def load_string(self):
        orig = self.readline()
        rep = orig[:-1]
        for q in (b'"', b"'"): # double or single quote
            if rep.startswith(q):
                if not rep.endswith(q):
                    raise ValueError("insecure string pickle")
                rep = rep[len(q):-len(q)]
                break
        else:
            raise ValueError("insecure string pickle: %r" % orig)
        self.append(codecs.escape_decode(rep)[0]
                    .decode(self.encoding, self.errors))
    dispatch[STRING[0]] = load_string

    def load_binstring(self):
        len = mloads(b'i' + self.read(4))
        data = self.read(len)
        value = str(data, self.encoding, self.errors)
        self.append(value)
    dispatch[BINSTRING[0]] = load_binstring

    def load_binbytes(self):
        len = mloads(b'i' + self.read(4))
        self.append(self.read(len))
    dispatch[BINBYTES[0]] = load_binbytes

    def load_unicode(self):
        self.append(str(self.readline()[:-1], 'raw-unicode-escape'))
    dispatch[UNICODE[0]] = load_unicode

    def load_binunicode(self):
        len = mloads(b'i' + self.read(4))
        self.append(str(self.read(len), 'utf-8', 'surrogatepass'))
    dispatch[BINUNICODE[0]] = load_binunicode

    def load_short_binstring(self):
        len = ord(self.read(1))
        data = bytes(self.read(len))
        value = str(data, self.encoding, self.errors)
        self.append(value)
    dispatch[SHORT_BINSTRING[0]] = load_short_binstring

    def load_short_binbytes(self):
        len = ord(self.read(1))
        self.append(bytes(self.read(len)))
    dispatch[SHORT_BINBYTES[0]] = load_short_binbytes

    def load_tuple(self):
        k = self.marker()
        self.stack[k:] = [tuple(self.stack[k+1:])]
    dispatch[TUPLE[0]] = load_tuple

    def load_empty_tuple(self):
        self.append(())
    dispatch[EMPTY_TUPLE[0]] = load_empty_tuple

    def load_tuple1(self):
        self.stack[-1] = (self.stack[-1],)
    dispatch[TUPLE1[0]] = load_tuple1

    def load_tuple2(self):
        self.stack[-2:] = [(self.stack[-2], self.stack[-1])]
    dispatch[TUPLE2[0]] = load_tuple2

    def load_tuple3(self):
        self.stack[-3:] = [(self.stack[-3], self.stack[-2], self.stack[-1])]
    dispatch[TUPLE3[0]] = load_tuple3

    def load_empty_list(self):
        self.append([])
    dispatch[EMPTY_LIST[0]] = load_empty_list

    def load_empty_dictionary(self):
        self.append({})
    dispatch[EMPTY_DICT[0]] = load_empty_dictionary

    def load_list(self):
        k = self.marker()
        self.stack[k:] = [self.stack[k+1:]]
    dispatch[LIST[0]] = load_list

    def load_dict(self):
        k = self.marker()
        d = {}
        items = self.stack[k+1:]
        for i in range(0, len(items), 2):
            key = items[i]
            value = items[i+1]
            d[key] = value
        self.stack[k:] = [d]
    dispatch[DICT[0]] = load_dict

    # INST and OBJ differ only in how they get a class object.  It's not
    # only sensible to do the rest in a common routine, the two routines
    # previously diverged and grew different bugs.
    # klass is the class to instantiate, and k points to the topmost mark
    # object, following which are the arguments for klass.__init__.
    def _instantiate(self, klass, k):
        args = tuple(self.stack[k+1:])
        del self.stack[k:]
        if (args or not isinstance(klass, type) or
            hasattr(klass, "__getinitargs__")):
            try:
                value = klass(*args)
            except TypeError as err:
                raise TypeError("in constructor for %s: %s" %
                                (klass.__name__, str(err)), sys.exc_info()[2])
        else:
            value = klass.__new__(klass)
        self.append(value)

    def load_inst(self):
        module = self.readline()[:-1].decode("ascii")
        name = self.readline()[:-1].decode("ascii")
        klass = self.find_class(module, name)
        self._instantiate(klass, self.marker())
    dispatch[INST[0]] = load_inst

    def load_obj(self):
        # Stack is ... markobject classobject arg1 arg2 ...
        k = self.marker()
        klass = self.stack.pop(k+1)
        self._instantiate(klass, k)
    dispatch[OBJ[0]] = load_obj

    def load_newobj(self):
        args = self.stack.pop()
        cls = self.stack[-1]
        obj = cls.__new__(cls, *args)
        self.stack[-1] = obj
    dispatch[NEWOBJ[0]] = load_newobj

    def load_global(self):
        module = self.readline()[:-1].decode("utf-8")
        name = self.readline()[:-1].decode("utf-8")
        klass = self.find_class(module, name)
        self.append(klass)
    dispatch[GLOBAL[0]] = load_global

    def load_ext1(self):
        code = ord(self.read(1))
        self.get_extension(code)
    dispatch[EXT1[0]] = load_ext1

    def load_ext2(self):
        code = mloads(b'i' + self.read(2) + b'\000\000')
        self.get_extension(code)
    dispatch[EXT2[0]] = load_ext2

    def load_ext4(self):
        code = mloads(b'i' + self.read(4))
        self.get_extension(code)
    dispatch[EXT4[0]] = load_ext4

    def get_extension(self, code):
        nil = []
        obj = _extension_cache.get(code, nil)
        if obj is not nil:
            self.append(obj)
            return
        key = _inverted_registry.get(code)
        if not key:
            raise ValueError("unregistered extension code %d" % code)
        obj = self.find_class(*key)
        _extension_cache[code] = obj
        self.append(obj)

    def find_class(self, module, name):
        # Subclasses may override this.
        if self.proto < 3 and self.fix_imports:
            if (module, name) in _compat_pickle.NAME_MAPPING:
                module, name = _compat_pickle.NAME_MAPPING[(module, name)]
            if module in _compat_pickle.IMPORT_MAPPING:
                module = _compat_pickle.IMPORT_MAPPING[module]
        __import__(module, level=0)
        mod = sys.modules[module]
        klass = getattr(mod, name)
        return klass

    def load_reduce(self):
        stack = self.stack
        args = stack.pop()
        func = stack[-1]
        try:
            value = func(*args)
        except:
            print(sys.exc_info())
            print(func, args)
            raise
        stack[-1] = value
    dispatch[REDUCE[0]] = load_reduce

    def load_pop(self):
        del self.stack[-1]
    dispatch[POP[0]] = load_pop

    def load_pop_mark(self):
        k = self.marker()
        del self.stack[k:]
    dispatch[POP_MARK[0]] = load_pop_mark

    def load_dup(self):
        self.append(self.stack[-1])
    dispatch[DUP[0]] = load_dup

    def load_get(self):
        i = int(self.readline()[:-1])
        self.append(self.memo[i])
    dispatch[GET[0]] = load_get

    def load_binget(self):
        i = self.read(1)[0]
        self.append(self.memo[i])
    dispatch[BINGET[0]] = load_binget

    def load_long_binget(self):
        i = mloads(b'i' + self.read(4))
        self.append(self.memo[i])
    dispatch[LONG_BINGET[0]] = load_long_binget

    def load_put(self):
        i = int(self.readline()[:-1])
        if i < 0:
            raise ValueError("negative PUT argument")
        self.memo[i] = self.stack[-1]
    dispatch[PUT[0]] = load_put

    def load_binput(self):
        i = self.read(1)[0]
        if i < 0:
            raise ValueError("negative BINPUT argument")
        self.memo[i] = self.stack[-1]
    dispatch[BINPUT[0]] = load_binput

    def load_long_binput(self):
        i = mloads(b'i' + self.read(4))
        if i < 0:
            raise ValueError("negative LONG_BINPUT argument")
        self.memo[i] = self.stack[-1]
    dispatch[LONG_BINPUT[0]] = load_long_binput

    def load_append(self):
        stack = self.stack
        value = stack.pop()
        list = stack[-1]
        list.append(value)
    dispatch[APPEND[0]] = load_append

    def load_appends(self):
        stack = self.stack
        mark = self.marker()
        list = stack[mark - 1]
        list.extend(stack[mark + 1:])
        del stack[mark:]
    dispatch[APPENDS[0]] = load_appends

    def load_setitem(self):
        stack = self.stack
        value = stack.pop()
        key = stack.pop()
        dict = stack[-1]
        dict[key] = value
    dispatch[SETITEM[0]] = load_setitem

    def load_setitems(self):
        stack = self.stack
        mark = self.marker()
        dict = stack[mark - 1]
        for i in range(mark + 1, len(stack), 2):
            dict[stack[i]] = stack[i + 1]

        del stack[mark:]
    dispatch[SETITEMS[0]] = load_setitems

    def load_build(self):
        stack = self.stack
        state = stack.pop()
        inst = stack[-1]
        setstate = getattr(inst, "__setstate__", None)
        if setstate:
            setstate(state)
            return
        slotstate = None
        if isinstance(state, tuple) and len(state) == 2:
            state, slotstate = state
        if state:
            inst_dict = inst.__dict__
            intern = sys.intern
            for k, v in state.items():
                if type(k) is str:
                    inst_dict[intern(k)] = v
                else:
                    inst_dict[k] = v
        if slotstate:
            for k, v in slotstate.items():
                setattr(inst, k, v)
    dispatch[BUILD[0]] = load_build

    def load_mark(self):
        self.append(self.mark)
    dispatch[MARK[0]] = load_mark

    def load_stop(self):
        value = self.stack.pop()
        raise _Stop(value)
    dispatch[STOP[0]] = load_stop

# Encode/decode longs.

def encode_long(x):
    r"""Encode a long to a two's complement little-endian binary string.
    Note that 0 is a special case, returning an empty string, to save a
    byte in the LONG1 pickling context.

    >>> encode_long(0)
    b''
    >>> encode_long(255)
    b'\xff\x00'
    >>> encode_long(32767)
    b'\xff\x7f'
    >>> encode_long(-256)
    b'\x00\xff'
    >>> encode_long(-32768)
    b'\x00\x80'
    >>> encode_long(-128)
    b'\x80'
    >>> encode_long(127)
    b'\x7f'
    >>>
    """
    if x == 0:
        return b''
    nbytes = (x.bit_length() >> 3) + 1
    result = x.to_bytes(nbytes, byteorder='little', signed=True)
    if x < 0 and nbytes > 1:
        if result[-1] == 0xff and (result[-2] & 0x80) != 0:
            result = result[:-1]
    return result

def decode_long(data):
    r"""Decode a long from a two's complement little-endian binary string.

    >>> decode_long(b'')
    0
    >>> decode_long(b"\xff\x00")
    255
    >>> decode_long(b"\xff\x7f")
    32767
    >>> decode_long(b"\x00\xff")
    -256
    >>> decode_long(b"\x00\x80")
    -32768
    >>> decode_long(b"\x80")
    -128
    >>> decode_long(b"\x7f")
    127
    """
    return int.from_bytes(data, byteorder='little', signed=True)

# Shorthands

def dump(obj, file, protocol=None, *, fix_imports=True):
    Pickler(file, protocol, fix_imports=fix_imports).dump(obj)

def dumps(obj, protocol=None, *, fix_imports=True):
    f = io.BytesIO()
    Pickler(f, protocol, fix_imports=fix_imports).dump(obj)
    res = f.getvalue()
    assert isinstance(res, bytes_types)
    return res

def load(file, *, fix_imports=True, encoding="ASCII", errors="strict"):
    return Unpickler(file, fix_imports=fix_imports,
                     encoding=encoding, errors=errors).load()

def loads(s, *, fix_imports=True, encoding="ASCII", errors="strict"):
    if isinstance(s, str):
        raise TypeError("Can't load pickle from unicode string")
    file = io.BytesIO(s)
    return Unpickler(file, fix_imports=fix_imports,
                     encoding=encoding, errors=errors).load()

# Use the faster _pickle if possible
try:
    from _pickle import *
except ImportError:
    Pickler, Unpickler = _Pickler, _Unpickler

# Doctest
def _test():
    import doctest
    return doctest.testmod()

if __name__ == "__main__":
    import sys, argparse
    parser = argparse.ArgumentParser(
        description='display contents of the pickle files')
    parser.add_argument(
        'pickle_file', type=argparse.FileType('br'),
        nargs='*', help='the pickle file')
    parser.add_argument(
        '-t', '--test', action='store_true',
        help='run self-test suite')
    parser.add_argument(
        '-v', action='store_true',
        help='run verbosely; only affects self-test run')
    args = parser.parse_args()
    if args.test:
        _test()
    else:
        if not args.pickle_file:
            parser.print_help()
        else:
            import pprint
            for f in args.pickle_file:
                obj = load(f)
                pprint.pprint(obj)
