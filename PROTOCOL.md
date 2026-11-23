# Wire protocol (version 1)

One TCP connection per client, client → host port 48100. Everything is framed:

```
+--------+----------------------+-----------------+
| type   | length (uint32, BE)  | payload         |
| 1 byte | 4 bytes              | `length` bytes  |
+--------+----------------------+-----------------+
```

| Type | Direction | Payload |
|---|---|---|
| `V` | host → client | UTF-8 JSON `{"version": 1, "width": W, "height": H}`: logical size of the streamed output in X pixels. Sent first, and again when the output size changes. |
| `F` | host → client | `>IHHHH` (shape id, width, height, hotspot x, hotspot y), then zlib-compressed pixels: width × height little-endian uint32, **premultiplied ARGB**, row by row. Width and height are at most 256. |
| `S` | host → client | `>I` id of the current shape; `0` means no visible cursor. |
| `P` | both ways | empty. The client sends one every 5 s and the host answers. Either side drops a peer that has been silent for 20 s. |

The host sends each shape (`F`) once per connection, before the first `S` that uses it. After that, a cursor change
costs one 9-byte `S` frame. Shape ids are stable for the life of the host process.

Scaling is up to the client. A shape is in host X pixels; the reference implementation scales it by
`video width on the client / W`, but never below 24 px, and never enlarges a cursor beyond its own size for that.

Unknown frame types must be ignored. A future version bumps `version` in `V`.
