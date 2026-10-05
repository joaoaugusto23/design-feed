# Job watcher: setup guide

This checks about 60 companies' job pages every 15 minutes and emails you when a new remote US product design role appears. It runs on GitHub for free, so it keeps working when your laptop is off.

Setup takes about 20 minutes. You only do it once.

## What's in this folder

- `watcher.py` is the program. You won't need to touch it.
- `settings.toml` controls which job titles and locations count as a match.
- `companies.txt` is the list of companies to watch.
- `workflow-file.yml` tells GitHub to run the program every 15 minutes.
- `claude-in-chrome-shortcut.txt` is the weekly Claude in Chrome task that finds new companies to add.

## Step 1: Get a Gmail app password

This is a special password that lets the watcher send email from your Gmail. It is not your normal password, and you can delete it any time.

1. Go to myaccount.google.com/security and make sure **2-Step Verification** is on. Google requires it for app passwords.
2. Go to myaccount.google.com/apppasswords.
3. Type the name `Job watcher` and click **Create**.
4. Google shows a 16-letter code. Copy it somewhere for a few minutes. You'll paste it in Step 5.

## Step 2: Create a GitHub account and a repository

A repository is just a folder on GitHub.

1. Sign up at github.com if you don't have an account. The free plan is enough.
2. Click the **+** in the top right, then **New repository**.
3. Name it `job-watcher`.
4. Choose **Public**. Public repositories get unlimited free running time. Anyone could see your list of companies, but your email address and password stay hidden (Step 5 keeps them secret). If you'd rather choose Private, change `*/15` to `*/30` in Step 4 so you stay within the free minutes.
5. Leave the other boxes unchecked and click **Create repository**.

## Step 3: Upload the files

1. On your new repository's page, click the link **uploading an existing file**.
2. Drag in these four files: `watcher.py`, `settings.toml`, `companies.txt`, `README.md`.
3. Click **Commit changes** at the bottom.

## Step 4: Add the schedule file

This one has to be created by hand, because it lives in a special folder.

1. On your repository page, click **Add file**, then **Create new file**.
2. In the file name box, type exactly: `.github/workflows/job-watcher.yml`
   (GitHub turns each `/` into a folder as you type. That's expected.)
3. Open `workflow-file.yml` from this folder on your computer, copy everything in it, and paste it into the big text box.
4. Click **Commit changes**, then **Commit changes** again in the pop-up.

## Step 5: Add your email details as secrets

Secrets are stored privately by GitHub. Nobody can see them, even on a public repository.

1. In your repository, click **Settings** (top menu), then on the left **Secrets and variables**, then **Actions**.
2. Click **New repository secret** and add these, one at a time:

| Name | What to paste |
|---|---|
| `GMAIL_ADDRESS` | Your Gmail address |
| `GMAIL_APP_PASSWORD` | The 16-letter code from Step 1 |
| `ALERT_TO` | Optional. Only if you want alerts sent to a different email address |

## Step 6: Run it once to test

1. Click the **Actions** tab. If GitHub asks, click the green button to enable workflows.
2. Click **Job watcher** on the left, then **Run workflow**, then the green **Run workflow** button.
3. Wait about a minute. A green check mark means it worked.
4. Check your email for **"Job watcher is set up"**. It lists matching roles that are open right now, plus any companies on the list that couldn't be checked.

After this, it runs by itself every 15 minutes. You'll only get an email when there's something new.

## Everyday use

**When an alert arrives:** click **Open and apply**. That's the company's own application page.

**To add a company:** open `companies.txt` on GitHub, click the pencil icon, paste a link to any job at that company on a new line, and click **Commit changes**. The next email will show that company's currently open roles under "Already open", and after that only new ones.

**To change which titles match:** edit `settings.toml` the same way. For example, delete `"manager"` from the skip list if you want design manager roles too.

**To fix companies that didn't work:** open `status.md` in your repository. It lists any company names that weren't found. Search Google for that company plus "greenhouse", "lever" or "ashby" to find its real job page, and paste that link into `companies.txt` instead. If a company doesn't use any of the three, delete its line.

**To pause it:** Actions tab, then **Job watcher**, then the **...** menu, then **Disable workflow**. Enable it again the same way.

## Weekly: let Claude in Chrome find more companies

1. Open `claude-in-chrome-shortcut.txt`, replace `MY-GITHUB-NAME` with your GitHub username, and save the text as a shortcut in Claude in Chrome.
2. Schedule it to run weekly with the clock icon in the extension panel.
3. When it finishes, paste the links it gives you at the bottom of `companies.txt`.

## Good to know

- GitHub sometimes runs scheduled jobs 5 to 20 minutes late when it's busy. You'll still usually hear about a role well before it spreads to LinkedIn.
- It only watches companies that use Greenhouse, Lever or Ashby. Many tech companies do, but some large companies use other systems.
- Roles marked "location doesn't say which country" just say "Remote". Check the listing before applying.
- If the run shows a red X, click it to see why. The most common cause is a mistyped app password in Step 5.
