# Landing page

`index.html` is the project's landing page — one self-contained file, no build step, no
dependencies beyond a Google Fonts stylesheet.

## Publishing it

**Settings → Pages → Build and deployment → Source: `GitHub Actions`.**

That is the only setting to change. `.github/workflows/pages.yml` then deploys this
folder on every push to `main`, and can be run on demand from **Actions → Pages → Run
workflow**.

If the site returns 404 while GitHub is still showing runs of a workflow called
`pages-build-deployment`, the source is still set to a branch: that workflow only exists
in branch mode, and it publishes the repository root, which has no `index.html`.

Why not "deploy from a branch"? That option only offers the repository root or `/docs` —
there is no way to select `site/`. And `/docs` already holds the architecture documents,
which are written to be read on GitHub rather than served as a website.

## Editing it

The page shows real output from the tool — the cost ledger, the DNS doctor report, the
personalisation verification gate. If those formats change, update the page. A landing
page showing output the software no longer produces is worse than no landing page.

The workflow validates the document structure before deploying (doctype, balanced
`<body>`, stylesheet inside `<head>`, every CSS custom property declared in the bare
`:root` block). Written by hand with no build step, those are exactly the mistakes that
would otherwise ship silently — two of them did, once.

## Known warning

The Pages deploy logs `Node.js 20 is deprecated ... actions/upload-artifact@v4`. That
comes from inside GitHub's own `upload-pages-artifact`, which still pins an
artifact-action version built for Node 20; the runner forces Node 24 and the deploy
succeeds. There is no newer release to bump to — it is tracked upstream in
actions/upload-pages-artifact#138 and actions/deploy-pages#410. Nothing to fix here.
