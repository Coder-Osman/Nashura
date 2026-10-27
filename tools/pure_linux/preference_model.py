import ctypes

KEY = "LOADING_SCREEN_STATE_LABEL"
CAPACITY = 8192
Codec = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint, ctypes.c_int,
                         ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t)
Contains = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint, ctypes.c_char_p,
                            ctypes.POINTER(ctypes.c_int))
Get = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint, ctypes.c_char_p,
                       ctypes.c_void_p, ctypes.c_size_t)
Edit = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint,
                        ctypes.POINTER(ctypes.c_uint64))
Change = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint64, ctypes.c_int,
                          ctypes.c_char_p, ctypes.c_char_p)
Commit = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint64,
                          ctypes.POINTER(ctypes.c_int))
DecodeException = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint)
Backup = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)


class Backend(ctypes.Structure):
    _fields_ = [("context", ctypes.c_void_p), ("codec", Codec), ("contains", Contains),
                ("get_string", Get), ("edit", Edit), ("change", Change), ("commit", Commit),
                ("decode_exception", DecodeException), ("request_backup", Backup)]


class PlatformFailure(Exception):
    pass


class DecodeFailure(Exception):
    pass


class Storage:
    def __init__(self, initial=None, failures=(), false_commits=()):
        self.memory = [dict(slot) for slot in (initial or [{}, {}])]
        self.disk = [dict(slot) for slot in self.memory]
        self.events = []
        self.failures = set(failures)
        self.false_commits = set(false_commits)
        self.editors = {}
        self.commits = 0

    def event(self, *event):
        self.events.append(list(event))
        if len(self.events) in self.failures:
            raise PlatformFailure()

    def codec(self, slot, operation, value):
        self.event("codec", slot, operation, value)
        if operation == 0:
            return f"encoded-{slot}:{value}"
        if operation == 1:
            return "cipher:" + value
        if not value.startswith("cipher:"):
            raise DecodeFailure()
        return value[7:]

    def contains(self, slot, key):
        self.event("contains", slot, key)
        return key in self.memory[slot]

    def get(self, slot, key):
        self.event("get_string", slot, key)
        value = self.memory[slot].get(key, "")
        if not isinstance(value, str):
            raise PlatformFailure()
        return value

    def edit(self, slot):
        self.event("edit", slot)
        token = len(self.editors) + 1
        self.editors[token] = [slot, None]
        return token

    def change(self, token, operation, key, value):
        self.event("change", token, operation, key, value)
        self.editors[token][1] = (operation, key, value)

    def commit(self, token):
        self.event("commit", token)
        self.commits += 1
        slot, (operation, key, value) = self.editors[token]
        if operation == 0:
            self.memory[slot][key] = value
        else:
            self.memory[slot].pop(key, None)
        success = self.commits not in self.false_commits
        if success:
            self.disk[slot] = dict(self.memory[slot])
        return success

    def decode_exception(self, slot):
        self.event("decode_exception", slot)

    def backup(self):
        self.event("backup")

    def adapter(self):
        def output(value, destination, capacity):
            data = value.encode() + b"\0"
            if len(data) > capacity:
                return -1
            ctypes.memmove(destination, data, len(data))
            return 0

        def codec(_, slot, operation, value, destination, capacity):
            try:
                return output(self.codec(slot, operation, value.decode()), destination, capacity)
            except DecodeFailure:
                return 1
            except PlatformFailure:
                return 1 if operation == 2 else -1

        def contains(_, slot, key, found):
            try:
                found[0] = self.contains(slot, key.decode())
                return 0
            except PlatformFailure:
                return -1

        def get(_, slot, key, destination, capacity):
            try:
                return output(self.get(slot, key.decode()), destination, capacity)
            except PlatformFailure:
                return -1

        def edit(_, slot, token):
            try:
                token[0] = self.edit(slot)
                return 0
            except PlatformFailure:
                return -1

        def change(_, token, operation, key, value):
            try:
                self.change(token, operation, key.decode(), value.decode() if value is not None else None)
                return 0
            except PlatformFailure:
                return -1

        def commit(_, token, boolean):
            try:
                boolean[0] = self.commit(token)
                return 0
            except PlatformFailure:
                return -1

        def exception(_, slot):
            try:
                self.decode_exception(slot)
                return 0
            except PlatformFailure:
                return -1

        def backup(_):
            try:
                self.backup()
                return 0
            except PlatformFailure:
                return -1

        return Backend(None, Codec(codec), Contains(contains), Get(get), Edit(edit),
                       Change(change), Commit(commit), DecodeException(exception), Backup(backup))


def configure(path):
    library = ctypes.CDLL(str(path))
    library.preference_contract_write.argtypes = [ctypes.POINTER(Backend), ctypes.c_char_p, ctypes.c_char_p]
    library.preference_contract_read.argtypes = [ctypes.POINTER(Backend), ctypes.c_char_p,
                                                ctypes.c_void_p, ctypes.c_size_t]
    return library


def invoke(library, storage, operation, value=None, key=KEY):
    adapter = storage.adapter()
    if operation == "write":
        return library.preference_contract_write(ctypes.byref(adapter), key.encode(),
                                                  value.encode() if value is not None else None), None
    output = ctypes.create_string_buffer(b"UNASSIGNED", CAPACITY)
    result = library.preference_contract_read(ctypes.byref(adapter), key.encode(), output, CAPACITY)
    return result, output.value.decode()


def encoded(slot):
    return f"encoded-{slot}:{KEY}"
