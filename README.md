debscout - find the fastest up-to-date Debian mirrors for apt
=============================================================

debscout tests Debian's official mirrors and CDNs from your own
connection, skips any that are out of date, measures real download
speed, and ranks them. If you ask it to, it points apt at the fastest
ones using apt's mirror list feature, so apt falls back to the next
mirror on its own if one fails. Every change is backed up and can be
undone with one command.

It needs Python 3.9 or newer and apt 1.6 or newer. Nothing else.


Synopsis
--------

    debscout [options]                  test mirrors, print a ranking
    debscout --apply [--dry-run] [-y]   switch apt to the fastest mirrors
    debscout --status                   show what debscout has configured
    debscout --undo                     put your original apt sources back


Why
---

deb.debian.org is a good default, but on some networks the route to
its CDN is very slow. On one machine in India it ran at 85-350 kB/s,
while other official mirrors on the same connection gave 10-22 MB/s.
A cold "apt-get update" took 116 seconds before debscout and 2.9
seconds after.

Picking a mirror by hand, or by ping time alone, goes wrong easily:

  - A mirror can answer pings quickly and still download slowly.
  - Some mirrors are hours, days or even months out of date.
  - A single fast test can be luck. Some mirrors swing between fast
    and slow from one minute to the next.

debscout checks all three.


Installation
------------

From the debscout apt repository (Debian 11 or newer, Ubuntu 22.04
or newer). Upgrades then come with the rest of your system:

    sudo curl -fsSLo /usr/share/keyrings/debscout.gpg \
        https://iam-super-user.github.io/debscout/debscout.gpg
    echo "deb [signed-by=/usr/share/keyrings/debscout.gpg] https://iam-super-user.github.io/debscout stable main" \
        | sudo tee /etc/apt/sources.list.d/debscout-repo.list
    sudo apt update
    sudo apt install debscout

The repository is signed with this key:

    rsa4096, created 2026-11-07, expires 2031-11-06
    fingerprint: 5D2B C8E3 028B 5D62 39C6  416E A08C 17C2 1C69 04A9

To check the key you downloaded:

    gpg --show-keys /usr/share/keyrings/debscout.gpg

From a .deb file, downloaded from the GitHub releases page:

    sudo apt install ./debscout_*_all.deb

From PyPI, with pipx. sudo does not search ~/.local/bin, so give it
the full path:

    pipx install debscout
    debscout
    sudo "$(command -v debscout)" --apply

From source, without installing anything:

    git clone https://github.com/iam-super-user/debscout.git
    cd debscout
    ./debscout
    sudo ./debscout --apply


Quick start
-----------

    debscout                    # test mirrors and print the ranking
    debscout --apply --dry-run  # show exactly what would change
    sudo debscout --apply       # switch apt to the fastest mirrors
    sudo debscout --undo        # put everything back

Testing needs no root and changes nothing. A full run takes about a
minute and a half.

Example run:

    $ debscout
    debscout 0.1.0: testing mirrors for Debian trixie (amd64)
    Debian mirror list: 351 mirrors (downloaded)
    Checked 353 mirrors in 16s: 345 up to date, 3 out of date, 5 unreachable
    Speed-testing 12 mirrors, 2 round(s) each (up to 8 MB or 5s per test)...

     #      Speed  Latency  Behind  Where  Mirror
     1  15.0 MB/s    14 ms    <1 h  BD     https://mirror.xeonbd.com/debian/
     2   9.3 MB/s    38 ms    <1 h  CDN    https://cdn-aws.deb.debian.org/debian/
     3   7.6 MB/s    76 ms    <1 h  SG     http://mirror.sg.gs/debian/
     4   5.8 MB/s    82 ms    <1 h  MY     http://debian.tuxuri.com/debian/
     ...
     7   1.6 MB/s    44 ms    <1 h  IN     http://debian.mirror.net.in/debian/  unstable
    12   118 kB/s    64 ms    <1 h  CDN    http://deb.debian.org/debian/

    Skipped because they are out of date (closest first):
      http://mirror.realcompute.io/debian/  18 h behind
      http://mirror.sjtu.edu.cn/debian/  2 days behind

    To switch apt to the top 3: sudo debscout --apply

"Speed" is total bytes over total time across all rounds. "Latency"
is the TCP connect time. "Behind" is how far the mirror lags the
newest mirror seen. "unstable" means the speed changed more than
four times between rounds.


Options
-------

Actions (pick one; the default is to test and print a ranking):

    --apply             switch apt to the fastest mirrors (needs root)
    --undo              restore the apt sources debscout changed
    --status            show what debscout has configured

Testing:

    -c, --country CC,...  only test mirrors in these countries, by ISO
                          code (e.g. IN,SG,HK); the CDNs are always tested
    -t, --test N          speed-test the N closest mirrors (default 10)
    --rounds N            speed-test rounds per mirror (default 2)
    --sample-mb MB        most MB to download per test (default 8)
    --time-limit SEC      most seconds per test download (default 5)
    --max-age HOURS       skip mirrors more than HOURS behind (default 12)
    --timeout SEC         timeout when checking mirrors (default 4)
    --suite SUITE         suite to test with (default: from your sources)
    --arch ARCH           architecture to test with (default: from dpkg)
    --no-cdn              don't consider Debian's CDNs

Applying:

    -n, --top N           put the N fastest mirrors in apt's list
                          (default 3); deb.debian.org is always added
                          last as a fallback
    --dry-run             with --apply: show the changes, make none
    --no-update           don't run "apt-get update" afterwards
    -y, --yes             don't ask for confirmation

Output:

    --json                print the ranking as JSON
    -v, --verbose         show every speed test
    -q, --quiet           only print results and errors
    -V, --version         print the version


How it works
------------

1. Candidates. Every mirror in Debian's official list
   (https://mirror-master.debian.org/status/Mirrors.masterlist) that
   carries your architecture, plus Debian's two CDNs: deb.debian.org
   (Fastly) and cdn-aws.deb.debian.org (Amazon CloudFront). The list
   is cached for a day.

2. Freshness. It reads each mirror's project/trace/master file, 32 at
   a time, and skips mirrors more than 12 hours behind the newest one.
   If a mirror redirects (often http to https), debscout keeps the
   final address, so apt doesn't pay for a redirect on every request.

3. Shortlist. It measures the TCP connect time to each up-to-date
   mirror, the best of two tries, and keeps the 10 closest plus the
   CDNs.

4. Speed test. It downloads up to 8 MB of your suite's Packages.xz
   from each shortlisted mirror, one mirror at a time so the tests
   don't compete, in two interleaved rounds.

5. Apply. It writes the 3 fastest mirrors, with deb.debian.org last
   as a fallback, to an apt mirror list. Before writing, it checks
   that each chosen mirror carries every suite you use.


What --apply changes
--------------------

  - It creates /etc/apt/sources.list.d/debscout.sources with the same
    suites, components and options as your Debian entries, pointing at
    mirror+file:/etc/apt/mirrors/debscout-debian.list.

  - It switches off your original Debian entries. Lines in .list files
    are commented out, and "Enabled: no" is added to .sources files.
    Other repositories (Chrome, Node.js, and so on) are never touched.

  - It backs up every file before changing it.

  - It runs "apt-get update". If that fails, everything is restored
    automatically.

Security updates only ever come from Debian's own hosts
(cdn-aws.deb.debian.org, security.debian.org, deb.debian.org), never
from third-party mirrors. Whichever mirror serves a file, apt still
checks Debian's signatures, so a mirror cannot change packages.

To re-rank later, run "sudo debscout --apply" again. The first backup
is kept, so "--undo" always returns you to where you started.

"sudo debscout --undo" restores every file exactly as it was. If you
edited one of those files after applying, your edited copy is saved
next to it as <name>.debscout-saved.


Files
-----

    /etc/apt/sources.list.d/debscout.sources
                            apt sources written by debscout
    /etc/apt/mirrors/debscout-debian.list
    /etc/apt/mirrors/debscout-debian-security.list
                            mirror lists, fastest first
    /var/lib/debscout/state.json
                            what was changed, for --status and --undo
    /var/lib/debscout/backup/
                            original copies of changed files
    /var/cache/debscout/Mirrors.masterlist
                            cached mirror list (~/.cache/debscout/
                            when not run as root)


Exit status
-----------

    0     success
    1     error (message on stderr)
    2     bad command-line usage
    130   interrupted with Ctrl-C


Troubleshooting
---------------

"--apply needs root"
    Run it with sudo. Use --apply --dry-run to preview without root.

apt is slow or failing after --apply
    Run "sudo debscout --apply" again to re-rank, or
    "sudo debscout --undo" to go back.

Behind a proxy
    debscout honours the http_proxy and https_proxy environment
    variables. sudo usually drops them, so use "sudo -E". apt's own
    proxy settings are not read.

A mirror you expected is missing
    Only mirrors in Debian's official list are tested. Mirrors more
    than --max-age hours behind are skipped and listed separately.


Uninstalling
------------

    sudo debscout --undo        # first, if you used --apply
    sudo apt remove debscout
    sudo rm /etc/apt/sources.list.d/debscout-repo.list \
            /usr/share/keyrings/debscout.gpg


Development
-----------

Run the tests (standard library only, no network needed):

    PYTHONPATH=src python3 -m unittest discover -s tests

Source layout:

    src/debscout/cli.py        command line and output
    src/debscout/mirrors.py    mirror list, freshness checks, speed tests
    src/debscout/sources.py    reading and writing apt sources
    src/debscout/apply.py      backups, apply, undo, rollback
    src/debscout/net.py        small HTTP helpers on urllib
    tests/                     unit and end-to-end tests
    debian/                    Debian packaging
    scripts/build-apt-repo.sh  builds the signed apt repository

Making a release:

    1. Bump __version__ in src/debscout/__init__.py.
    2. Add a debian/changelog entry with the same version.
    3. Commit, then tag and push: git tag v0.1.1 && git push --tags

The release workflow runs the tests, builds the .deb and the Python
package, and publishes to PyPI, GitHub releases and the apt
repository.


Reporting bugs
--------------

Open an issue at https://github.com/iam-super-user/debscout/issues
and include:

  - the output of "debscout --version" and "debscout -v"
  - your release (cat /etc/os-release)
  - the files in /etc/apt/sources.list.d/, if --apply went wrong


See also
--------

apt-transport-mirror(1), sources.list(5), netselect-apt(1).

debscout was inspired by Manjaro's "pacman-mirrors --fasttrack" and
Arch Linux's reflector. It is an independent implementation and
shares no code with them.


License
-------

MIT. See the LICENSE file.
