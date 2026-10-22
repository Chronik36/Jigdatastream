# Publishing BotTrack updates

The code repository is private, so BotTrack checks for updates in a small **public** repository
that only holds releases (`Chronik36/BotTrack-releases`). Every build of `main` publishes the
.dmg and the app code there, and installed copies update from it with no token.

One-time setup:

1. Create the repository at https://github.com/new: owner Chronik36, name `BotTrack-releases`,
   **Public**, and tick **Add a README file** (a release needs at least one commit).
2. Create a token at https://github.com/settings/personal-access-tokens/new:
   - Repository access: **Only select repositories** → `BotTrack-releases`
   - Permissions → Repository permissions → **Contents: Read and write**
   - Pick the longest expiration you're comfortable with, and note the date to renew it.
3. In Jigdatastream, open Settings → Secrets and variables → Actions → **New repository secret**,
   name it `RELEASES_TOKEN` (or `BOTTRACK`), and paste the token.

After that, each push or merge to `main` builds the app and publishes a release named
`build-N-<commit>`. To publish without a code change, run the "Build BotTrack for Mac" workflow
on `main` from the Actions tab.

Anything in a release is public: the .dmg and the app's Python code. Settings, inboxes, orders,
cards and keys never leave the user's Mac.
