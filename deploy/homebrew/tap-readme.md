# JobsFR Homebrew Tap

JobsFR searches French job boards and exports a spreadsheet to track your
applications.

```sh
brew tap mandaloriany/tap
brew trust --formula mandaloriany/tap/jobsfr
brew install mandaloriany/tap/jobsfr
jobsfr setup
jobsfr search
```

The default tracker is `~/Documents/jobsfr/jobs.xlsx`. Run `jobsfr status`
to find your local configuration file.

See the [deployment guide](https://github.com/MandalorianY/jobsfr/blob/main/deploy/README.md#macos-with-homebrew)
for scheduled runs.
