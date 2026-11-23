# Contributors

arrstack-mcp exists because people send patches, report bugs and test it against
real stacks. Everyone listed here has code or documentation in the repository,
credited by commit authorship or a `Co-Authored-By:` trailer.

## Code & documentation

| Contributor | Contributions |
|-------------|---------------|
| **Esequiel Albornoz** ([@Esequiel378](https://github.com/Esequiel378)) | Whole-item delete tools (`radarr_delete_movie`, `sonarr_delete_series`, `seerr_delete_request`), the `qbt_delete` split into `qbt_remove_torrent` / `qbt_remove_torrent_and_files` so each action can be gated separately, quality-profile selection on Seerr requests, and the percent-encoding fix for `seerr_search`. (#20, #21) |
| **Olindo Pindaro** ([@pindaroli](https://github.com/pindaroli)) | qBittorrent category, tag, tracker, file-inspection, recheck and reannounce tools (#16); Prowlarr application management, sync and indexer-stats tools (#17); qBittorrent 5.2+ session-cookie fix (#6); fork contributions and guided configuration (#11). |
| **Cody Cook** ([@didyouexpectthat](https://github.com/didyouexpectthat)) | MCP SDK compatibility and the Jellyfin authentication fix (#9); series titles in Sonarr upcoming episodes (#10). |
| **lechu515** ([@lechu515](https://github.com/lechu515)) | Radarr interactive search (`radarr_releases`, `radarr_grab_release`) and usenet support in `prowlarr_grab`, ported in #15. |
| **Brian Phelps** ([@BPCP](https://github.com/BPCP)) | Fork contributions, log redaction and guided configuration (#11). |
| **Bryan Feller** ([@bfeller](https://github.com/bfeller)) | Fork contributions, log redaction and guided configuration (#11). |
| **Justin Tout** ([@justintout](https://github.com/justintout)) | Optional services and the configurable tool catalog; game-library support and MCP transport hardening. |

## How credit is recorded

- Pull requests are merged, not squashed, so every contributor's commits keep
  their original authorship.
- Where work is ported or reworked, the commit carries a
  `Co-Authored-By: Name <email>` trailer so the original author is credited
  alongside the person who landed it.
- Releases name contributors in their notes, and this file lists everyone who
  has landed code in the tree.

If you contributed and are missing or misdescribed here, open an issue or a PR
against this file — it is meant to be the durable record.
