"""Scalar event identities with structurally shared, independent snapshots."""

from collections.abc import MutableSet


def _insert(node, value, code, shift=0):
    if node is None:
        return (code, frozenset((value,)))
    if isinstance(node, tuple):
        if node[0] == code:
            return node if value in node[1] else (code, node[1] | {value})
        branch = (node[0] >> shift) & 31
        node = {branch: node}
    branch = (code >> shift) & 31
    child = node.get(branch)
    updated = _insert(child, value, code, shift + 5)
    if child is updated:
        return node
    return {**node, branch: updated}


def _discard(node, value, code, shift=0):
    if node is None:
        return None
    if isinstance(node, tuple):
        if node[0] != code or value not in node[1]:
            return node
        values = node[1] - {value}
        return (code, values) if values else None
    branch = (code >> shift) & 31
    child = node.get(branch)
    updated = _discard(child, value, code, shift + 5)
    if updated is child:
        return node
    result = dict(node)
    if updated is None:
        del result[branch]
    else:
        result[branch] = updated
    return result or None


class SnapshotEventSet(MutableSet):
    """Hash trie: copies share nodes; writes replace only their own hash path."""

    __slots__ = ("_root", "_size")

    def __init__(self, values=()):
        self._root = None
        self._size = 0
        for value in values:
            self.add(value)

    def __contains__(self, value):
        code, shift, node = hash(value), 0, self._root
        while isinstance(node, dict):
            node = node.get((code >> shift) & 31)
            shift += 5
        return node is not None and node[0] == code and value in node[1]

    def __len__(self):
        return self._size

    def __iter__(self):
        pending = [self._root]
        while pending:
            node = pending.pop()
            if isinstance(node, dict):
                pending.extend(node.values())
            elif node is not None:
                yield from node[1]

    def add(self, value):
        if type(value) is not tuple or any(type(part) not in (int, str, type(None)) for part in value):
            raise ValueError("public tracker event identities must be immutable scalar tuples")
        if value not in self:
            self._root = _insert(self._root, value, hash(value))
            self._size += 1

    def discard(self, value):
        if value in self:
            self._root = _discard(self._root, value, hash(value))
            self._size -= 1

    def clear(self):
        self._root, self._size = None, 0

    def copy(self):
        result = type(self)()
        result._root, result._size = self._root, self._size
        return result

    def __copy__(self):
        return self.copy()

    def __deepcopy__(self, memo):
        result = self.copy()
        memo[id(self)] = result
        return result
