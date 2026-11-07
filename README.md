# debscout

**Find the fastest up-to-date Debian mirrors for your connection, and set apt up to use them, with automatic fallback.**

> Version 0.1, in testing. Packaged releases (.deb and an apt repository) are coming next.

## Why

`deb.debian.org` is a great default, but on some networks the route to its CDN is very slow. On one real machine in India:

| | `apt-get update` (33 MB, cold) |
|---|---|
| deb.debian.org (default) | **116 s** at ~286 kB/s |
| After debscout | **2.9 s** at ~14.7 MB/s |

Existing tools don't catch this:

- **netselect-apt** ranks mirrors by ping time. A mirror can answer pings quickly and still download slowly. It also doesn't check whether a mirror is up to date.
- Some mirrors are days, or even months, behind. A mirror picker has to check freshness.

## Usage

```
debscout                    # test mirrors and show the ranking (no root needed)
debscout -c IN,SG,HK        # only test mirrors in these countries (plus the CDNs)
debscout --apply --dry-run  # preview the exact changes to your apt sources
sudo debscout --apply       # switch apt to the fastest mirrors
sudo debscout --status      # show what debscout has configured
sudo debscout --undo        # put your original apt sources back
```

Example output:

```
 #      Speed  Latency  Behind  Where  Mirror
 1  15.0 MB/s    13 ms    <1 h  BD     https://mirror.xeonbd.com/debian/
 2  10.1 MB/s    36 ms    <1 h  CDN    https://cdn-aws.deb.debian.org/debian/
 3   6.4 MB/s    77 ms    <1 h  SG     http://mirror.sg.gs/debian/
 ...
11   271 kB/s    62 ms    <1 h  CDN    http://deb.debian.org/debian/

Skipped because they are out of date (closest first):
  http://mirror.sjtu.edu.cn/debian/  2 days behind
```

Run `debscout --help` for all options.

## How it works

1. **Candidates:** every mirror in Debian's official list ([Mirrors.masterlist](https://mirror-master.debian.org/status/Mirrors.masterlist)) that carries your architecture, plus Debian's two CDNs: `deb.debian.org` (Fastly) and `cdn-aws.deb.debian.org` (Amazon CloudFront).
2. **Freshness:** reads each mirror's `project/trace/master` file in parallel and skips any mirror more than 12 hours behind the newest. Redirects are followed and the final address is kept, so apt doesn't pay for a redirect on every request.
3. **Shortlist:** measures the network latency (TCP connect time) to each up-to-date mirror and keeps the 10 closest, plus the CDNs.
4. **Speed test:** downloads up to 8 MB of your suite's package index from each shortlisted mirror, one at a time, in two rounds. A mirror's score is total bytes divided by total time, so a mirror that is fast once and slow once ranks down, and big swings are flagged as "unstable".
5. **Apply:** writes the 3 fastest mirrors, with `deb.debian.org` as the final fallback, to an apt mirror list. apt tries them in order and moves to the next one automatically if one fails.

### What `--apply` changes

- Creates `/etc/apt/sources.list.d/debscout.sources`, with the same suites, components and options as your Debian entries, pointing at `mirror+file:/etc/apt/mirrors/debscout-debian.list`.
- Creates the mirror lists in `/etc/apt/mirrors/`.
- Switches off your original Debian entries: comments out `.list` lines, or adds `Enabled: no` in `.sources` files. Other repositories (Chrome, Node.js and so on) are never touched.
- Backs up every file before changing it, in `/var/lib/debscout/`.
- Runs `apt-get update`. If that fails, everything is restored automatically.

**Security updates** only ever come from Debian's own hosts (`security.debian.org`, `deb.debian.org` and `cdn-aws.deb.debian.org`), never from third-party mirrors. Package integrity is always guaranteed by apt's signature checks, whichever mirror serves the files.

`sudo debscout --undo` restores every file exactly as it was. If you edited a file after applying, your edited version is kept beside it as `*.debscout-saved`.

## Install (from source, for now)

Needs Python 3.9 or newer and nothing else. It uses only the standard library.

```
git clone https://github.com/iam-super-user/debscout.git
cd debscout
./debscout                 # run in place
sudo ./debscout --apply
```

## Development

```
PYTHONPATH=src python3 -m unittest discover -s tests
```

## Inspired by

- Manjaro's `pacman-mirrors --fasttrack`
- Arch Linux's `reflector`
- Debian's `netselect-apt`

debscout is an independent implementation and shares no code with these projects.

## License

MIT. See [LICENSE](LICENSE).
