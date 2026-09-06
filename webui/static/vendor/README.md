# Vendored libraries

Checked in rather than loaded from a CDN, for three reasons: the UI works with
no network, the repository is a complete artifact you can read, and there is no
build step here to fetch them for you.

| file | version | why |
| --- | --- | --- |
| `cytoscape.min.js` | 3.30.2 | draws the graph, and gives us click/select for free |
| `dagre.min.js` | 0.8.5 | layered layout — a peer dependency of the next one |
| `cytoscape-dagre.min.js` | 2.5.0 | plugs dagre in as a cytoscape layout |

Cytoscape rather than React Flow (which the reference UI uses) only because
there is no build step: cytoscape ships a UMD bundle that works from a plain
`<script>` tag, and React Flow does not.

To update one, download the same file from the same CDN path with the version
changed, and update the table above.
