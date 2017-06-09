﻿from gevent import monkey
monkey.patch_all(thread=False, select=False)

import logging
import os
import praw
import re
import random
import sqlite3
import time
import traceback
import warnings

import requests

from bs4 import BeautifulSoup
from html.parser import unescape
from urllib.parse import urlencode

from praw.errors import APIException, ClientException, HTTPException

USER_AGENT = "Archives to archive.is and archive.org (/r/SnapshillBot) v1.3"
INFO = "/r/SnapshillBot"
CONTACT = "/message/compose?to=\/r\/SnapshillBot"
ARCHIVE_ORG_FORMAT = "%Y%m%d%H%M%S"
MEGALODON_JP_FORMAT = "%Y-%m%d-%H%M-%S"
DB_FILE = os.environ.get("DATABASE", "snapshill.sqlite3")
LEN_MAX = 35
REDDIT_API_WAIT = 2
WARN_TIME = 300 # warn after spending 5 minutes on a post
REDDIT_PATTERN = re.compile("https?://(([A-z]{2})(-[A-z]{2})"
                            "?|beta|i|m|pay|ssl|www)\.?reddit\.com")
SUBREDDIT_OR_USER = re.compile("/(u|user|r)/[^\/]+/?$")
PLAIN_SUBREDDIT_OR_USER = re.compile("^/?(u|user|r)/[^\/]+/?$")
# we have to do some manual ratelimiting because we are tunnelling through
# some other websites.

RECOVERABLE_EXC = (APIException,
                   ClientException,
                   HTTPException)

ERROR_MESSAGE = "could not auto-archive; click to resubmit!"


loglevel = logging.DEBUG if os.environ.get("DEBUG") == "true" else logging.INFO

logging.basicConfig(level=loglevel,
                    format="[%(asctime)s] [%(levelname)s] %(message)s")

log = logging.getLogger("snapshill")
logging.getLogger("requests").setLevel(loglevel)
warnings.simplefilter("ignore")  # Ignore ResourceWarnings (because screw them)

r = praw.Reddit(USER_AGENT)
ignorelist = set()

s = requests.Session()

def get_footer():
    return "*^(I am a bot.) ^\([*Info*]({info}) ^/ ^[*Contact*]({" \
           "contact}))*".format(info=INFO, contact=CONTACT)




def ratelimit(url):
    if len(re.findall(REDDIT_PATTERN, url)) == 0:
        return
    time.sleep(REDDIT_API_WAIT)


def refresh_ignore_list():
    ignorelist.clear()
    ignorelist.add(r.user.name)
    for friend in r.user.get_friends():
        ignorelist.add(friend.name)


def fix_url(url):
    """
    Change language code links, mobile links and beta links, SSL links and
    username/subreddit mentions
    :param url: URL to change.
    :return: Returns a fixed URL
    """
    if re.match(PLAIN_SUBREDDIT_OR_USER, url):
        url = "http://www.reddit.com/" + url

    return re.sub(REDDIT_PATTERN, "http://www.reddit.com", url)


def skip_url(url):
    """
    Skip naked username mentions and subreddit links.
    """
    if REDDIT_PATTERN.match(url) and SUBREDDIT_OR_USER.search(url):
        return True

    return False


def log_error(e):
    log.error("Unexpected {}:\n{}".format(e.__class__.__name__,
                                          traceback.format_exc()))


class Archive:
    site_name = None

    def __init__(self, url):
        self.url = url
        self.archived = None

    def archive(self):
        return None

    def name(self):
        if self.archived:
            return self.site_name
        else:
            return "_{}\*_".format(self.site_name)

    def link(self):
        return self.archived or self.error_message()

    def resubmit_link(self):
        return None

    def error_message(self):
        return "{} \"{}\"".format(self.resubmit_link(), ERROR_MESSAGE)

    def format(self):
        return "[{}]({})".format(self.name(), self.link())


class ArchiveIsArchive(Archive):
    site_name = "archive.is"

    def archive(self):
        """
        Archives to archive.is. Returns a 200, and we have to find the
        JavaScript redirect through a regex in the response text.
        :return: URL of the archive or False if an error occurred
        """
        pairs = {"url": self.url}

        try:
            res = s.post("https://archive.is/submit/", pairs, verify=False)
        except RECOVERABLE_EXC:
            return False

        found = re.findall("http[s]?://archive.is/[0-z]{1,6}", res.text)

        if len(found) < 1:
            return False

        return found[0]

    def error_link(self):
        pairs = {"url": self.url, "run": 1}
        return "https://archive.is/?" + urlencode(pairs)


class ArchiveOrgArchive(Archive):
    site_name = "archive.org"

    def archive(self):
        """
        Archives to archive.org. The website gives a 403 Forbidden when the
        archive cannot be generated (because it follows robots.txt rules)
        :return: URL of the archive, False if an error occurred, or None if
        we cannot archive this page.
        """
        try:
            s.get("https://web.archive.org/save/" + self.url)
        except RECOVERABLE_EXC as e:
            if isinstance(e, HTTPError) and e.status_code == 403:
                return None
            return False
        date = time.strftime(ARCHIVE_ORG_FORMAT, time.gmtime())
        return "https://web.archive.org/" + date + "/" + self.url

    def error_link(self):
        return "https://web.archive.org/save/" + self.url


class MegalodonJPArchive(Archive):
    site_name = "megalodon.jp"

    def archive(self):
        """
        Archives to megalodon.jp. The website gives a 302 redirect when we
        POST to the webpage. We can't guess the link because a 1 second
        discrepancy will give an error when trying to view it.
        :return: URL of the archive, or False if an error occurred.
        """
        pairs = {"url": self.url}

        try:
            res = s.post("http://megalodon.jp/pc/get_simple/decide", pairs)
        except RECOVERABLE_EXC:
            return False

        if res.url == "http://megalodon.jp/pc/get_simple/decide":
            return False

        return res.url

    def error_link(self):
        return "http://megalodon.jp/pc/get_simple/decide?url={}".format(self.url)


class GoldfishArchive(Archive):
    site_name = "snew.github.io"

    def archive(self):
        return re.sub(REDDIT_PATTERN, "https://snew.github.io", self.url)


class Link:
    def __init__(self, url, text):
        log.debug("Creating Link {}".format(url))
        self.url = url
        self.text = (text[:LEN_MAX] + "...") if len(text) > LEN_MAX else text
        self.archives = [ArchiveOrgArchive(url),
                         MegalodonJPArchive(url)]

        if re.match(REDDIT_PATTERN, url):
            self.archives.append(GoldfishArchive(url))

        self.archives.append(ArchiveIsArchive(url))

    def format(self, no=1):
        archives = ", ".join(
            archive.format() for archive in self.archives if archive.archived is not None
        )

        return "{}. [{}]({}) - {}".format(no, self.text, self.url, archives)


class Post:
    def __init__(self, submission):
        self.submission = submission
        self.links = []

    def should_notify(self):
        """
        Looks for other snapshot bot comments in the comment chain and doesn't
        post if they do.
        :param submission: Submission to check
        :return: If we should comment or not
        """
        cur.execute("SELECT * FROM links WHERE id=?", (self.name,))

        if cur.fetchone():
            return False

        return True

    @property
    def name(self):
        return self.submission.name

    @property
    def permalink(self):
        return self.submission.permalink

    def format_links(self):
        lines = []

        for i, link in enumerate(self.links, 1):
            lines.append(link.format(i))

        return "\n".join(lines)

    def add_comment(self, *args, **kwargs):
        self.submission.add_comment(*args, **kwargs)


class Notification:

    def __init__(self, post, header, links):
        self.post = post
        self.header = header
        self.links = links

    def notify(self):
        """
        Replies with a comment containing the archives or if there are too
        many links to fit in a comment, post a submisssion to
        /r/SnapshillBotEx and then make a comment linking to it.
        :return Nothing
        """
        try:
            comment = self._build()

            if len(comment) > 9999:
                link = self.post.permalink
                submission = r.submit("SnapshillBotEx", "Archives for " + link,
                                      text=comment[:39999],
                                      raise_captcha_exception=True)
                submission.add_comment("The original submission can be found "
                                       "here:\n\n" + link)
                comment = self.post.add_comment("Wow, that's a lot of links! The "
                                          "snapshots can be [found here.](" +
                                          submission.url + ")\n\n" + get_footer())
                log.info("Posted a comment and new submission")
            else:
                comment = self.post.add_comment(comment)

        except RECOVERABLE_EXC as e:
            log_error(e)
            return

        cur.execute("INSERT INTO links (id, reply) VALUES (?, ?)",
                    (self.post.name, comment.name))

    def _build(self):
        parts = [self.header.get(), "Snapshots:"]
        format = "[{name}]({archive})"

        for i, link in enumerate(self.links, 1):
            subparts = []
            log.debug("Found link")

            for archive in link.archives:
                if archive.archived is None:
                    continue

                archive_link = archive.archived

                if not archive_link:
                    log.debug("Not found, using error link")
                    archive_link = archive.error_link + ' "could not ' \
                                                        'auto-archive; ' \
                                                        'click to resubmit it!"'
                else:
                    log.debug("Found archive")

                subparts.append(format.format(name=archive.name,
                                              archive=archive_link))

            parts.append("{}. {} - {}".format(i, link.text, ", ".join(subparts)))

        parts.append(get_footer())

        return "\n\n".join(parts)


class Header:

    def __init__(self, settings_wiki, subreddit):
        self.subreddit = subreddit
        self.texts = []
        self._settings = r.get_subreddit(settings_wiki)

        try:
            content = self._get_wiki_content()
            if not content.startswith("!ignore"):
                self.texts = self._parse_quotes(content)
        except RECOVERABLE_EXC:
            pass

    def __len__(self):
        return len(self.texts)

    def get(self):
        """
        Gets a random message from the extra text or nothing if there are no
        messages.
        :return: Random message or an empty string if the length of "texts"
        is 0.
        """
        return "" if not self.texts else random.choice(self.texts)

    def _get_wiki_content(self):
        return self._settings.get_wiki_page("extxt/" + self.subreddit.lower()).content_md

    def _parse_quotes(self, quotes_str):
        return [q.strip() for q in re.split('\r\n-{3,}\r\n', quotes_str) if q.strip()]


class Snapshill:

    def __init__(self, username, password, settings_wiki, limit=25):
        self.username = username
        self.password = password
        self.limit = limit
        self.settings_wiki = settings_wiki
        self.headers = {}
        self._setup = False

    def run(self):
        """
        Checks through the submissions and archives and posts comments.
        """
        if not self._setup:
            raise Exception("Snapshiller not ready yet!")

        raw_subs = r.get_new(limit=self.limit)

        for raw_sub in raw_subs:
            submis

            log.debug("Found submission.\n" + submission.permalink)

            if not should_notify(submission):
                log.debug("Skipping.")
                continue

            archives = [ArchiveContainer(fix_url(submission.url),
                                         "*This Post*")]
            if submission.is_self and submission.selftext_html is not None:
                log.debug("Found text post...")

                links = BeautifulSoup(unescape(
                    submission.selftext_html)).find_all("a")

                if not len(links):
                    continue

                finishedURLs = []

                for anchor in links:
                    log.debug("Found link in text post...")

                    url = fix_url(anchor['href'])

                    if skip_url(url):
                        continue

                    if url in finishedURLs:
                        continue #skip for sanity

                    archives.append(ArchiveContainer(url, anchor.contents[0]))
                    finishedURLs.append(url)
                    # ratelimit(url)

            for container in archives:
                container.run()

            Notification(submission, self._get_header(submission.subreddit),
                         archives).notify()
            db.commit()

    def setup(self):
        """
        Logs into reddit and refreshs the header text and ignore list.
        """
        self._login()
        self.refresh_headers()
        refresh_ignore_list()
        self._setup = True

    def quit(self):
        self.headers = {}
        self._setup = False

    def refresh_headers(self):
        """
        Refreshes the header text for all subreddits.
        """
        self.headers = {"all": Header(self.settings_wiki, "all")}
        # for subreddit in r.get_my_subreddits():
        #     name = subreddit.display_name.lower()
        #     self.headers[name] = Header(self.settings_wiki, name)

    def _login(self):
        r.login(self.username, self.password)

    def _get_header(self, subreddit):
        """
        Gets the correct Header object for this subreddit. If the one for 'all'
        is not "!ignore", then this one will always be returned.
        :param subreddit: Subreddit object to get.
        :return: Extra text object found or the one for "all" if we can't find
        it or if not empty.
        """
        all = self.headers["all"]

        if len(all):
            return all  # return 'all' one for announcements

        return self.headers.get(subreddit.display_name.lower(), all)


db = sqlite3.connect(DB_FILE)
cur = db.cursor()

if __name__ == "__main__":
    username = os.environ.get("REDDIT_USER")
    password = os.environ.get("REDDIT_PASS")
    limit = int(os.environ.get("LIMIT", 25))
    wait = int(os.environ.get("WAIT", 5))
    refresh = int(os.environ.get("REFRESH", 1800))

    log.info("Starting...")
    snapshill = Snapshill(username, password, "SnapshillBot", limit)
    snapshill.setup()

    log.info("Started.")
    try:
        cycles = 0
        while True:
            try:
                cycles += 1
                log.info("Running")
                snapshill.run()
                log.info("Done")
                # This will refresh by default around ~30 minutes (depending
                # on delays).
                if cycles > (refresh / wait) / 2:
                    log.info("Reloading header text and ignore list...")
                    refresh_ignore_list()
                    snapshill.refresh_headers()
                    cycles = 0
            except RECOVERABLE_EXC as e:
                log_error(e)

            time.sleep(wait)
    except KeyboardInterrupt:
        pass
    snapshill.quit()
    db.close()
    exit(0)
