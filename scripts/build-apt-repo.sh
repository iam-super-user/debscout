#!/bin/sh
# Build a signed apt repository from .deb files.
#
#   usage: scripts/build-apt-repo.sh DEB_DIR OUT_DIR
#
# The private signing key is read (ASCII-armoured) from $APT_SIGNING_KEY.
# Users install with:
#   deb [signed-by=/usr/share/keyrings/debscout.gpg] https://iam-super-user.github.io/debscout stable main
set -eu

DEBS=$(cd "$1" && pwd)
OUT=$2
SUITE=stable
COMPONENT=main
# debscout is "Architecture: all", so the same index serves every architecture.
ARCHES="amd64 arm64 armel armhf i386 loong64 mips64el ppc64el riscv64 s390x"
REPO_URL=https://iam-super-user.github.io/debscout

if [ -z "${APT_SIGNING_KEY:-}" ]; then
    echo "error: APT_SIGNING_KEY is not set" >&2
    exit 1
fi

rm -rf "$OUT"
mkdir -p "$OUT/pool/$COMPONENT"
cp "$DEBS"/*.deb "$OUT/pool/$COMPONENT/"
cd "$OUT"

for arch in all $ARCHES; do
    dir="dists/$SUITE/$COMPONENT/binary-$arch"
    mkdir -p "$dir"
    apt-ftparchive packages "pool/$COMPONENT" > "$dir/Packages"
    gzip -9nk "$dir/Packages"
    xz -9k "$dir/Packages"
done

apt-ftparchive \
    -o APT::FTPArchive::Release::Origin=debscout \
    -o APT::FTPArchive::Release::Label=debscout \
    -o APT::FTPArchive::Release::Suite=$SUITE \
    -o APT::FTPArchive::Release::Codename=$SUITE \
    -o "APT::FTPArchive::Release::Architectures=all $ARCHES" \
    -o APT::FTPArchive::Release::Components=$COMPONENT \
    -o "APT::FTPArchive::Release::Description=debscout apt repository" \
    release "dists/$SUITE" > Release.tmp
mv Release.tmp "dists/$SUITE/Release"

GNUPGHOME=$(mktemp -d)
export GNUPGHOME
trap 'rm -rf "$GNUPGHOME"' EXIT
printf '%s\n' "$APT_SIGNING_KEY" | gpg --batch --quiet --import
FPR=$(gpg --batch --with-colons --list-secret-keys | awk -F: '/^fpr:/ {print $10; exit}')
gpg --batch --yes --local-user "$FPR" --clearsign -o "dists/$SUITE/InRelease" "dists/$SUITE/Release"
gpg --batch --yes --local-user "$FPR" --armor --detach-sign -o "dists/$SUITE/Release.gpg" "dists/$SUITE/Release"
gpg --batch --export "$FPR" > debscout.gpg
gpg --batch --armor --export "$FPR" > debscout.asc

VERSION=$(apt-ftparchive packages "pool/$COMPONENT" | awk '/^Version:/ {print $2; exit}')
cat > index.html <<EOF
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>debscout apt repository</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 46rem; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; color: #1a1a1a; background: #fff; }
  pre { background: #f4f4f4; padding: 1rem; overflow-x: auto; border-radius: 6px; }
  code { font-size: 0.95em; }
  @media (prefers-color-scheme: dark) { body { color: #e6e6e6; background: #161616; } pre { background: #262626; } a { color: #8ab4ff; } }
</style>
</head>
<body>
<h1>debscout apt repository</h1>
<p><a href="https://github.com/iam-super-user/debscout">debscout</a> finds the fastest up-to-date Debian mirrors for your connection and configures apt to use them, with automatic fallback. Current version: <strong>$VERSION</strong>.</p>
<h2>Install</h2>
<pre><code>sudo curl -fsSLo /usr/share/keyrings/debscout.gpg $REPO_URL/debscout.gpg
echo "deb [signed-by=/usr/share/keyrings/debscout.gpg] $REPO_URL $SUITE $COMPONENT" | sudo tee /etc/apt/sources.list.d/debscout-repo.list
sudo apt update
sudo apt install debscout</code></pre>
<h2>Use</h2>
<pre><code>debscout                  # test mirrors and show the ranking
sudo debscout --apply     # switch apt to the fastest mirrors
sudo debscout --undo      # put your original apt sources back</code></pre>
<p>Signing key fingerprint: <code>$FPR</code></p>
</body>
</html>
EOF
touch .nojekyll

echo "Built signed apt repository in $OUT (key $FPR, debscout $VERSION)"
