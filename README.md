# debscout

**Find the fastest up-to-date Debian mirrors for your connection, and set apt up to use them, with automatic fallback.**

> 🚧 Work in progress. Not ready for use yet.

## Why

`deb.debian.org` is a great default, but on some networks the route to its CDN is very slow. On one real machine in India:

| Source | Download speed |
|---|---|
| deb.debian.org | 85–185 KB/s (occasionally 18 MB/s) |
| cdn-aws.deb.debian.org | ~22 MB/s, consistently |

A full `apt-get update` took **116 s** before and **10 s** after switching. Package downloads went from roughly 7–15 minutes for an 80 MB package to under 4 seconds.

Existing tools don't catch this:

- **netselect-apt** ranks mirrors by ping time. A mirror can answer pings quickly and still download slowly. It also doesn't check whether a mirror is up to date.
- The nearest, quickest-to-respond mirror in that test was **5 months out of date**. A mirror picker has to check freshness.

## How it will work

1. **Candidates:** Debian's official mirror list (`Mirrors.masterlist`), plus Debian's CDNs (`deb.debian.org`, `cdn-aws.deb.debian.org`).
2. **Freshness:** reads each mirror's `project/trace/master` timestamp and rejects mirrors that are behind.
3. **Shortlist:** a quick parallel connection-time check picks the most promising mirrors.
4. **Speed test:** real downloads of a few MB from each shortlisted mirror, repeated, because a single test can mislead.
5. **Output:** an apt mirror list (`mirror+file:`) in priority order. apt falls back to the next mirror automatically if one fails.
6. **Safety:** backs up your sources, only uses official Debian hosts for security updates, and can undo with one command.
7. **No root needed** for testing. Root is only needed to apply the result.

## Inspired by

- Manjaro's `pacman-mirrors --fasttrack`
- Arch Linux's `reflector`
- Debian's `netselect-apt`

debscout is an independent implementation and shares no code with these projects.

## License

MIT. See [LICENSE](LICENSE).
