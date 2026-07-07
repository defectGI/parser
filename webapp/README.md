# webapp/

Developer interface. Shows every stage of the parser pipeline (raw → parse → image handling →
table describe) step by step, clearly and simply.

- Start-stop mode: each stage waits for a button click; once a stage finishes, it stops and
  shows the results; clicking the button again moves on to the next stage.
- Results should be navigable: e.g. clicking text that references an image should be able to
  jump to that image/location.
- Design: simple, low on color, clear, clean, self-explanatory.

Note: this interface only covers the parser stages; the chunking/RAPTOR stages are out of
scope for this repo.
