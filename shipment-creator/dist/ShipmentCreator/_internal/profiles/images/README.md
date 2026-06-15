# Dispatcher avatars

Drop one image per dispatcher here, named by the profile **id** (lowercase). The app
shows the active dispatcher's image in the sidebar, under the "Destination state"
button.

Expected filenames (png/jpg/jpeg/webp/gif all work):

| File         | Profile | Image you sent |
| ------------ | ------- | -------------- |
| `soyo.png`   | Soyo    | Jigglypuff     |
| `kelly.png`  | Kelly   | Cinccino       |
| `duka.png`   | Duka    | Pichu          |
| `burte.png`  | Burte   | Emolga         |

(The mapping above is the order you pasted them in — rename the files however you
like; the filename is what binds an image to a profile, e.g. `kelly.png` → Kelly.)

The app serves these via `GET /api/profile-image/<id>`; a missing image just hides
the avatar — no error.
