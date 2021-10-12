import os
import re
import sh
import time
import configparser

from pathlib import Path
from github import Github
from dingtalkchatbot.chatbot import DingtalkChatbot
from sh import git


CURR_DIR = Path(__file__).parent.absolute()

dingtalk_access_token = os.getenv("DING_ACCESS_TOKEN")
dingtalk_secret = os.getenv("DING_SECRET")
dingtalk_bot = DingtalkChatbot(
    webhook=f"https://oapi.dingtalk.com/robot/send?access_token={dingtalk_access_token}",
    secret=dingtalk_secret,
)

gh_url = "https://github.com"

token = os.getenv('GH_PAT')
gh = Github(token)

prog = re.compile(r"(.*)\(#(\d+)\)(?:$|\n).*")
title_re = re.compile(r"(.*)(?:$|\n).*")


class Commit:
    def __init__(self, commit = None):
        self.commit = commit
        self.title = None
        self.pr_num = -1
        self.extract_pr_num_and_title(commit)

    def author(self):
        assert self.is_valid()
        return self.commit.commit.author

    def login(self):
        assert self.is_valid()
        return self.commit.author.login

    def is_valid(self):
        return self.commit is not None and (self.pr_num >= 0 or self.title is not None)

    def has_same_title(self, ci):
        return self.title.lower() == ci.title.lower()

    def extract_pr_num_and_title(self, commit):
        if commit is None:
            return
        msg = prog.match(commit.commit.message)
        if msg:
            self.title = msg.group(1).strip()
            self.pr_num = int(msg.group(2))
        else:
            msg = title_re.match(commit.commit.message)
            if msg:
                self.title = msg.group(1).strip()


def get_org_members(org_name):
    print(">>> Get org members")
    org = gh.get_organization(org_name)
    return [m.login for m in org.get_members()]


def init(clone_url, ent_dir):
    print(">>> Clone and config enterprise repo")
    if ent_dir.exists():
        sh.rm("-rf", ent_dir)
    git.clone("--single-branch", clone_url, ent_dir)
    with sh.pushd(ent_dir):
        git.config("user.name", "nebula-bots")
        git.config("user.email", "nebula-bots@vesoft.com")


def must_create_dir(filename):
    dirname = os.path.dirname(filename)
    if len(dirname) > 0 and not os.path.exists(dirname):
        sh.mkdir('-p', dirname)


def overwrite_conflict_files(ci, ent_dir):
    print(">>> Overwrite PR conflict files")
    with sh.pushd(ent_dir):
        for f in ci.files:
            if f.status == "removed":
                git.rm('-rf', f.filename)
            else:
                must_create_dir(f.filename)
                sh.curl("-fsSL", f.raw_url, "-o", f.filename)
            print(f"      {f.filename}")


def commit_changes(ci: Commit):
    author = ci.author()
    print(f">>> Commit changes by <{author.email}>")
    git.add(".")
    git.commit("-m", ci.title, "--author", f"{author.name} <{author.email}>")


def apply_patch(branch, comm_ci, ent_dir):
    print(f">>> Apply patch file to {branch}")
    stopped = False
    with sh.pushd(ent_dir):
        patch = f"{branch}.patch"
        git.fetch("origin", "master")
        git.checkout("-b", branch, "origin/master")
        git_commit = comm_ci.commit
        sh.curl("-fsSL", git_commit.html_url+'.patch', "-o", patch)
        try:
            git.am("--3way", patch)
            sh.rm("-rf", patch)
        except Exception:
            sh.rm("-rf", patch)
            overwrite_conflict_files(git_commit, ent_dir)
            commit_changes(comm_ci)
            stopped = True
        git.push("-u", "origin", branch)
    return stopped


def find_latest_community_commit_in_ent_repo(ent_commit: Commit, community_commits):
    assert ent_commit.is_valid()
    for ci in community_commits:
        assert ci.is_valid()
        if ent_commit.has_same_title(ci):
            return ci
    return Commit()


def generate_latest_100_commits(repo):
    commits = []
    for i, ci in enumerate(repo.get_commits()):
        if i > 100:
            break
        commit = Commit(repo.get_commit(ci.sha))
        if commit.is_valid():
            commits.append(commit)
    return commits


def find_unmerged_community_commits_in_ent_repo(community_repo, ent_repo):
    ent_commits = generate_latest_100_commits(ent_repo)
    community_commits = generate_latest_100_commits(community_repo)
    for ent_commit in ent_commits:
        ci = find_latest_community_commit_in_ent_repo(ent_commit, community_commits)
        if ci.is_valid():
            return community_commits[:community_commits.index(ci)]
    return []


def pr_ref(repo, pr):
    pr_num = pr if isinstance(pr, int) else pr.number
    return "{}#{}".format(repo.full_name, pr_num)


def pr_link(repo, pr):
    pr_num = pr if isinstance(pr, int) else pr.number
    return "[{}]({}/{}/pull/{})".format(pr_ref(repo, pr_num), gh_url, repo.full_name, pr_num)


def append_migration_in_msg(repo, pr):
    body = pr.body if pr.body else ""
    return "{}\n\nMigrated from {}\n".format(body, pr_ref(repo, pr))


def notify_author_by_comment(ent_repo, comm_ci, issue_num, comm_pr_num, org_members):
    comment = ""
    if comm_ci.login() in org_members:
        comment += f"@{comm_ci.login()}\n"
        print(f">>> Notify the author by comment: {comm_ci.login()}")
    else:
        print(f">>> The author {comm_ci.login()} is not in the orgnization, need not to notify him")

    comment += """This PR will cause conflicts when applying patch.
Please carefully compare all the changes in this PR to avoid overwriting legal codes.
If you need to make changes, please make the commits on current branch.

You can use following commands to resolve the conflicts locally:

```shell
$ git clone git@github.com:vesoft-inc/nebula-ent.git
$ cd nebula-ent
$ git checkout -b pr-{} origin/master
$ curl -fsSL "{}.patch" -o {}.patch
$ git am -3 {}.patch
# resolve the conflicts
$ git am --continue
$ git push -f origin pr-{}
```
"""

    issue = ent_repo.get_issue(issue_num)
    issue.create_comment(comment.format(comm_pr_num, comm_ci.commit.html_url, comm_pr_num, comm_pr_num, comm_pr_num))


def create_pr(comm_repo, ent_repo, comm_ci, org_members, ent_dir):
    try:
        merged_pr = comm_repo.get_pull(comm_ci.pr_num)
        branch = "pr-{}".format(merged_pr.number)
        stopped = apply_patch(branch, comm_ci, ent_dir)
        body = append_migration_in_msg(comm_repo, merged_pr)
        new_pr = ent_repo.create_pull(title=comm_ci.title, body=body, head=branch, base="master")

        print(f">>> Create PR: {pr_ref(ent_repo, new_pr)}")
        time.sleep(2)

        new_pr = ent_repo.get_pull(new_pr.number)
        new_pr.add_to_labels('auto-sync')

        if stopped:
            notify_author_by_comment(ent_repo, comm_ci, new_pr.number, comm_ci.pr_num, org_members)
            return (False, new_pr.number)

        if not new_pr.mergeable:
            return (False, new_pr.number)

        commit_title = "{} (#{})".format(comm_ci.title, new_pr.number)
        status = new_pr.merge(merge_method='squash', commit_title=commit_title)
        if not status.merged:
            return (False, new_pr.number)
        return (True, new_pr.number)

    except Exception as e:
        print(e)
        return (False, -1)


def get_org_name(repo):
    l = repo.split('/')
    assert len(l) == 2
    return l[0]


def get_repo_name(repo):
    l = repo.split('/')
    assert len(l) == 2
    return l[1]


def main(community_repo, enterprise_repo):
    comm_repo = gh.get_repo(community_repo)
    ent_repo = gh.get_repo(enterprise_repo)

    ent_dir = CURR_DIR / get_repo_name(enterprise_repo)

    org_members = get_org_members(get_org_name(community_repo))
    init(ent_repo.clone_url.replace("github.com", f"{token}@github.com"), ent_dir)

    unmerged_community_commits = find_unmerged_community_commits_in_ent_repo(comm_repo, ent_repo)
    unmerged_community_commits.reverse()

    succ_pr_list = []
    err_pr_list = []
    for ci in unmerged_community_commits:
        res = create_pr(comm_repo, ent_repo, ci, org_members, ent_dir)
        md = pr_link(comm_repo, ci.pr_num)
        if res[1] >= 0:
            md += " -> " + pr_link(ent_repo, res[1])
        md += " " + ci.login()
        if res[0]:
            succ_pr_list.append(md)
            print(f">>> {pr_ref(ent_repo, res[1])} has been migrated from {pr_ref(comm_repo, ci.pr_num)}")
        else:
            err_pr_list.append(md)
            print(f">>> {pr_ref(comm_repo, ci.pr_num)} could not be merged into {ent_repo.full_name}")
            break

    succ_prs = '\n\n'.join(succ_pr_list) if succ_pr_list else "None"
    err_prs = '\n\n'.join(err_pr_list) if err_pr_list else "None"

    if len(succ_pr_list) > 0 or len(err_pr_list) > 0:
        text = f"### Auto Merge Status\nMerge successfully:\n\n{succ_prs}\n\nFailed to merge:\n\n{err_prs}"
        dingtalk_bot.send_markdown(title='Auto Merge Status', text=text, is_at_all=False)

    if len(unmerged_community_commits) == 0:
        print(">>> There's no any PRs to sync")

    if ent_dir.exists():
        sh.rm("-rf", ent_dir)


if __name__ == "__main__":
    config = configparser.ConfigParser()
    config.read(CURR_DIR / 'repos.ini')
    for section in config.sections():
        print(">>> Start to sync section: {}".format(section))
        main(config[section]['community_repo'], config[section]['enterprise_repo'])
