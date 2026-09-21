# Building Documentation

Documentation is generated locally by an agent, not included in the source tree
or published package. Bootstrap creates the bilingual MkDocs source,
configuration, and site, so it can take some time. Only
[docs/AGENTS.md](docs/AGENTS.md) and the Chinese changelog are tracked; all
other documentation files are generated output.

## Prerequisites

Requires Docker, an installed package, its image, and an OpenAI-compatible model
provider:

```bash
pip install .                 # provides xunc
make build-docker             # first build can take time
```

Export the provider variables in the shell:

```bash
export XUN_OPENAI_API_KEY=your-api-key
export XUN_OPENAI_BASE_URL=https://api.example.com/v1
export XUN_OPENAI_MODEL=your-model
```

`xunc` forwards them into the container; never put credentials in generated
documentation.

## Bootstrap or Update

Run the documentation agent in a container from the repository root:

```bash
make doc
```

The agent follows [docs/AGENTS.md](docs/AGENTS.md) and builds the site into
`site/`.

## Package the Site

After `site/` has been generated, copy it into the package assets:

```bash
make doc-dist
```

This is a no-op before bootstrap. When present, `WebDisplayService` serves the
packaged site publicly at `/docs/`.

## Clean Generated Files

To discard generated documentation and restore the tracked bootstrap files:

```bash
make doc-clean
```

Do not hand-edit `site/` or `src/xun/assets/docs/`; both are generated output.