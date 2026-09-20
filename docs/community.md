# Community tricks

Installing tricks other people wrote, and publishing your own.

[← back to the README](../README.md)

---

## Community Tricks

Tricks are shareable. Anyone can publish one, and they show up in everyone's dashboard within the hour, in the **Available Tricks** list on the Tricks tab, next to your local ones.

**There is no registry server.** The index is a static `index.json` in [day50-dev/tricks](https://github.com/day50-dev/tricks), rebuilt hourly by a GitHub Action that crawls public repos carrying the topic `petsitter-trick`. No accounts, no approval queue, nothing to keep running.

### Installing

From the dashboard, hit **Install** on any community entry. It downloads, verifies the checksum, and adds it to the selected trickset. Or from the CLI:

```bash
pet search tool                  # search the index
pet cat dana/ollama-ctx          # read the source first
pet install dana/ollama-ctx --trickset opencode
```

Installed tricks land at `<config>/tricks/<owner>/<slug>/<version>.py`, and tricksets refer to them with a `pkg:` spec rather than a path:

```json
{
  "name": "my-trickset",
  "tricks": [
    "tricks/json_mode.py",
    "pkg:dana/ollama-ctx@0.1.0"
  ]
}
```

The `pkg:` form is what makes a trickset portable. The same JSON works on another machine, where a `/home/you/...` path would not. Omit `@version` and the newest installed version is used.

Point at a different index (a private one for your org, say) with `PET_REGISTRY_INDEX`, either an `https://` or a `file://` URL. The index is cached for an hour; a stale cache is preferred to an error, so the list still works offline.

### Publishing

Three steps, no ceremony:

1. **Put a `__version__` on your Trick subclass.** Semver, bumped whenever the file changes.
2. **Push it to a public GitHub repo.** Root or a `tricks/` directory, as many tricks per repo as you like.
3. **Add the topic:** `gh repo edit --add-topic petsitter-trick`

`pet publish tricks/my_trick.py` runs steps 2 and 3 for you and checks step 1 first.

```python
class OllamaCtxTrick(Trick):
    __version__ = "0.1.0"
    __brief__ = "Clamps num_ctx for ollama backends"
    __display_name__ = "Ollama Context Clamp"
```

Everything in the index is derived from that file and the GitHub API. You never type a checksum, a date, or an author:

| Field | Where it comes from |
|-------|---------------------|
| `name` | your GitHub login + the filename, e.g. `dana/ollama-ctx` |
| `version` | `__version__` |
| `brief`, `display_name` | `__brief__`, `__display_name__` (else the class name) |
| `keywords`, `prompt_keyword`, `required_models` | the class attributes |
| `url` | pinned to a commit SHA, so the bytes can never change under someone |
| `sha256` | computed from those bytes; `pet install` refuses on a mismatch |
| `repo`, `stars`, `license`, `updated` | the GitHub API |

Names can't collide between authors, because your GitHub login is the namespace, so there is nothing for anyone to adjudicate and publishing needs no permission. The crawler parses candidate files with `ast`; it never imports or executes them.

To update, bump `__version__` and push. To unpublish, delete the repo or drop the topic. Anyone who already installed it keeps their copy, since the file is on their disk.

`featured.json` in the index repo controls which tricks appear before you click **Show N more community tricks**. It's promotion, not permission: nothing is ever kept out of the index for being unfeatured.

> A trick is Python that runs inside petsitter with your API keys, the same trust model as any pip package. `pet cat` and the dashboard's **Read** button exist because a trick is one short file, a good deal more reviewable than the average dependency.

