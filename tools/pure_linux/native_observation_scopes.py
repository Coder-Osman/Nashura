"""Track per-thread observation scopes without assuming single-threaded execution."""


class ObservationScopes:
    def __init__(self):
        self.active = {}

    def enter(self, name, thread):
        key = (name, thread)
        if key in self.active:
            raise ValueError("nested observation scope")
        self.active[key] = True

    def leave(self, name, thread):
        key = (name, thread)
        if key not in self.active:
            raise ValueError("missing observation scope")
        del self.active[key]

    def contains(self, name, thread):
        return (name, thread) in self.active

    def any(self, name):
        return any(scope == name for scope, _ in self.active)
