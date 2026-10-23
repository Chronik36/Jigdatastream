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

4. For the Android app: add two more repository secrets, `ANDROID_KEYSTORE_B64` and
   `ANDROID_KEYSTORE_PASSWORD`, from the signing key Claude made (the files in the project's
   `order-tracker/android-signing/` folder). Every Android build is signed with this one key, so a new
   version installs over the old one. Keep a copy somewhere safe: if the key is lost, testers have to
   uninstall the app once to move to a new key. Without these secrets releases simply leave the app out.

After that, each push or merge to `main` builds the app and publishes a release named
`build-N-<commit>`. To publish without a code change, run the "Build BotTrack for Mac" workflow
on `main` from the Actions tab.

When anything in `android/` changes, raise `versionCode` in `android/app/build.gradle`. Releases carry
the app as `BotTrack-android-<versionCode>.apk`, and phones running an older number are offered it.

Anything in a release is public: the .dmg, the Android app and the app's Python code. Settings, inboxes, orders,
cards and keys never leave the user's Mac.
