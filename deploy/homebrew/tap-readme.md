# JobsFR Homebrew Tap

JobsFR searches French job boards and exports a spreadsheet to track your
applications.

```sh
brew tap mandaloriany/tap
brew install mandaloriany/tap/jobsfr
jobsfr
```

The default tracker is `~/Documents/job-scrapper/jobs.xlsx`. Run `jobsfr --status`
to find your local configuration file.

If you installed `job-scrapper` before the rename, use the upgrade instructions
in the [deployment guide](https://github.com/MandalorianY/french-job-scraper/blob/main/deploy/README.md#macos-with-homebrew).
