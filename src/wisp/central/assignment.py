from __future__ import annotations


def responsible_users(device_id: int, parents: dict[int, int | None],
                      assignments: dict[int, set[int]]) -> set[int]:


    seen: set[int] = set()
    out: set[int] = set()
    cur: int | None = device_id
    while cur is not None and cur not in seen:
        seen.add(cur)
        out |= assignments.get(cur, set())
        cur = parents.get(cur)
    return out


def audience_for(device_id: int, parents: dict[int, int | None],
                 assignments: dict[int, set[int]]) -> set[int] | None:

    users = responsible_users(device_id, parents, assignments)
    return users or None


def scope_of(user_id: int, parents: dict[int, int | None],
             assignments: dict[int, set[int]]) -> set[int]:
    roots = {did for did, users in assignments.items() if user_id in users}
    if not roots:
        return set()
    children: dict[int, list[int]] = {}
    for did, parent in parents.items():
        if parent is not None:
            children.setdefault(parent, []).append(did)
    out: set[int] = set(roots)
    stack = list(roots)
    while stack:
        cur = stack.pop()
        for kid in children.get(cur, ()):
            if kid not in out:
                out.add(kid)
                stack.append(kid)
    return out


class PagingAudience:


    def __init__(self, store, org_id: str) -> None:
        self.store = store
        self.org_id = org_id
        self._loaded = False
        self._parents: dict[int, int | None] = {}
        self._assignments: dict[int, set[int]] = {}
        self._owner_numbers: list[str] = []
        self._worker_numbers: dict[int, str] = {}
        self._cache: dict[int, list[str]] = {}


    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        self._parents = self.store.device_parent_map(self.org_id)
        self._assignments = self.store.device_assignment_map(self.org_id)
        numbers = self.store.org_paging_numbers(self.org_id)
        self._owner_numbers = numbers["owners"]
        self._worker_numbers = numbers["workers"]

    def _compose(self, worker_numbers) -> list[str]:
        return list(dict.fromkeys([*self._owner_numbers, *worker_numbers]))

    def _workers(self, users) -> list[str]:
        return [self._worker_numbers[u] for u in sorted(users)
                if u in self._worker_numbers]


    def owners_only(self) -> list[str]:
        self._load()
        return self._compose([])

    def for_device(self, device_id: int | None) -> list[str]:
        if device_id is None:
            return self.owners_only()
        cached = self._cache.get(device_id)
        if cached is not None:
            return cached
        self._load()
        users = audience_for(device_id, self._parents, self._assignments) or set()
        out = self._compose(self._workers(users))
        self._cache[device_id] = out
        return out

    def for_node(self, node_id: str) -> list[str]:

        self._load()
        users: set[int] = set()
        for did in self.store.node_device_ids(self.org_id, node_id):
            users |= responsible_users(did, self._parents, self._assignments)
        return self._compose(self._workers(users))
