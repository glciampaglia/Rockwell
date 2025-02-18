import os
import re
import gzip
import html
import numpy as np
from dateutil import parser
from flask import Flask, render_template, request, url_for, redirect, flash, make_response, jsonify
import requests
import random, string
import datetime
from requests_oauthlib import OAuth1Session
#from src.databaseAccess.database_config import config
from configparser import ConfigParser
from collections import defaultdict
import src.feedGeneration.CardInfo as CardInfo
import src.authorizer.ratelimiter as ratelimiter
import logging
import time
import psycopg2
import json
import glob
import xml
import xml.sax.saxutils
from sqlitedict import SqliteDict #to save session data

app = Flask(__name__)

app.debug = True

#LOG_FMT_DEFAULT='%(asctime)s:%(levelname)s:%(message)s'
#LOG_PATH_DEFAULT="/home/rockwell/Rockwell/backend/src/authorizer/authorizer.log"

log_level = logging.DEBUG
logging.basicConfig(filename='authorizer.log', level=log_level)
NG_FILE_LOCATION="/home/rockwell/Rockwell/backend/src/recsys/NewsGuardIffy/label-2022101916.json"

HOMETIMELINE_CAP = 400
USERTIMELINE_CAP = 300
FAVORITES_CAP = 200
TIMELINE_CAP = 100

def config(filename='database.ini', section='postgresql'):
    # create a parser
    parser = ConfigParser()
    # read config file
    parser.read(filename)
    # get section, default to postgresql
    db = {}
    if parser.has_section(section):
        params = parser.items(section)
        for param in params:
            db[param[0]] = param[1]
    else:
        raise Exception('Section {0} not found in the {1} file'.format(section, filename))

    return db

def get_hoaxy_engagement(user_id,hoaxy_config):

    """
    Returns a list of JSON that represents the tweets of the user inside the Hoaxy database.
    Args:
        user_id: The user ID of the user whose tweets you want to get.
    Returns:
        A list of JSON that represents the tweets of the user.
    """

    res = []
    hostname  = str(hoaxy_config['host'])
    port_id = str(hoaxy_config['port'])
    db = str(hoaxy_config['database'])
    username = str(hoaxy_config['user'])
    pwd = str(hoaxy_config['password'])
    conn = None
    cur = None

    err_message = "NA"

    try:
        conn = psycopg2.connect (
            host = hostname,
            dbname =db,
            user = username,
            password = pwd,
            port = port_id,
        )

        cur =  conn.cursor()
        script = """ select tweet.json_data from tweet join ass_tweet_url on tweet.id = ass_tweet_url.tweet_id join url on url.id = ass_tweet_url.url_id where user_id = placeholder; """

        script = script.replace("placeholder", str(user_id))
        cur.execute(script)


        for element in cur.fetchall():
            res.append(element[0])

    except Exception as err:
        err_message = err

    finally:
        if cur is not None:
            cur.close()

        if conn is not None:
            conn.close()
    return res,err_message

def contains_video(tweet):
    if tweet.get("entities"):
        if tweet.get("entities").get("media"):
            for media in tweet.get("entities")["media"]:
                if media.get("type") == "video":
                    return True
    return False

def compose_queries_512_chars (username:str) -> list:
    """
        This function compose the queries and makes sure the length does not surpass 512

        Agrs:
            username: this is the user name of the current user
            filename: this is the json file containing the urls of the news domains

        Retunrns:
            a list of the queries
    """

    queries = []
    count = 0

    file = open(NG_FILE_LOCATION)
    data = json.load(file)

    #print(data[0])
    query = 'from:'+ username + ' (url:".' +data[0]['identifier'] + '/"' + ' url:"//' + data[0]['identifier'] + '/"'
    #query += 'OR url: .' + data[0]['identifierAlt'] +'OR url: //' + data[0]['identifierAlt']

    for i in range(1, len(data) - 1):
        domain_alt = ""
        domain = data[i]['identifier']
        try:
            domain_alt =  data[i]['identifierAlt']
        except:
            count += 1

        if len(query) + (len(domain) + 13) < 512:
            query += ' OR url:".' + domain + '/"'
        else:
            queries.append(query + ")")
            query = 'from:'+ username +' (url:".' + domain + '/"'

        if len(query) + (len(domain) + 14) < 512:
            query += ' OR url:"//' + domain + '/"'
        else:
            queries.append(query + ")")
            query = 'from:'+ username +' (url:"//' + domain + '/"'

        if len(domain_alt) > 1:
            if len(query) + (len(domain_alt) + 13) < 512:
                query += ' OR url:".' + domain_alt + '/"'
            else:
                queries.append(query + ")")
                query = 'from:'+ username +' (url:".' + domain_alt + '/"'

            if len(query) + (len(domain_alt) + 14) < 512:
                query += ' OR url:"//' + domain_alt + '/"'
            else:
                queries.append(query + ")")
                query = 'from:'+ username +' (url:"//' + domain_alt + '/"'

    file.close()

    queries_test = []
    for i in range(200):
        queries_test.append(queries[i])

    for qq in queries:
        if "abcnews" in qq:
            queries_test.append(qq)
    return queries_test

webInformation = config('../configuration/config.ini','webconfiguration')

app_callback_url = str(webInformation['callback'])
app_callback_url_qual = str(webInformation['qualcallbackv2'])
request_token_url = str(webInformation['request_token_url'])
access_token_url = str(webInformation['access_token_url'])
authorize_url = str(webInformation['authorize_url'])
rockwell_url = str(webInformation['app_route'])
account_settings_url = str(webInformation['account_settings_url'])
creation_date_url = str(webInformation['creation_date_url'])

timeline_params = {
    "tweet.fields" : "id,text,edit_history_tweet_ids,attachments,author_id,conversation_id,created_at,entities,in_reply_to_user_id,lang,public_metrics,referenced_tweets,reply_settings",
    "user.fields" : "id,name,username,created_at,description,entities,location,pinned_tweet_id,profile_image_url,protected,public_metrics,url,verified",
    "media.fields": "media_key,type,url,duration_ms,height,preview_image_url,public_metrics,width",
    "expansions" : "author_id,referenced_tweets.id,attachments.media_keys"
}

timeline_params_engagement = {
    "tweet.fields" : "id,text,edit_history_tweet_ids,attachments,author_id,conversation_id,created_at,entities,in_reply_to_user_id,lang,public_metrics,referenced_tweets,reply_settings",
    "user.fields" : "id,name,username,created_at,description,entities,location,pinned_tweet_id,profile_image_url,protected,public_metrics,url,verified",
    "media.fields": "media_key,type,url,duration_ms,height,preview_image_url,public_metrics,width",
    "expansions" : "author_id,referenced_tweets.id,attachments.media_keys",
    "max_results" : 100
}

# oauth_store = {}
# start_url_store = {}
# screenname_store = {}
# userid_store = {}
# worker_id_store = {}
# access_token_store = {}
# access_token_secret_store = {}
# max_page_store = {}
# session_id_store = {}
# twitterversion_store = {}
# mode_store = {}
# participant_id_store = {}
# assignment_id_store = {}
# project_id_store = {}
# completed_survey = {}

# this the chosen database name
db_name = "sessionData.sqlite"
oauth_store = SqliteDict(db_name, tablename="oauth_store", autocommit=True)
start_url_store = SqliteDict(db_name, tablename="start_url_store", autocommit=True)
screenname_store = SqliteDict(db_name, tablename="screenname_store ", autocommit=True)
userid_store = SqliteDict(db_name, tablename="userid_store ", autocommit=True)
worker_id_store = SqliteDict(db_name, tablename="worker_id_store ", autocommit=True)
access_token_store = SqliteDict(db_name, tablename="access_token_store ", autocommit=True)
access_token_secret_store = SqliteDict(db_name, tablename="access_token_secret_store", autocommit=True)
max_page_store = SqliteDict(db_name, tablename="max_page_store", autocommit=True)
session_id_store = SqliteDict(db_name, tablename="session_id_store", autocommit=True)
twitterversion_store = SqliteDict(db_name, tablename="twitterversion_store", autocommit=True)
mode_store = SqliteDict(db_name, tablename="mode_store", autocommit=True)
participant_id_store = SqliteDict(db_name, tablename="participant_id_store", autocommit=True)
assignment_id_store = SqliteDict(db_name, tablename="assignment_id_store", autocommit=True)
project_id_store = SqliteDict(db_name, tablename="project_id_store", autocommit=True)
hometimeline_pulled_store = SqliteDict(db_name, tablename="hometimeline_pulled_store", autocommit=True)
completed_survey = SqliteDict(db_name, tablename="completed_survey", autocommit=True)
experimental_condition = SqliteDict(db_name, tablename="experimental_condition", autocommit=True)

def close_database_connections() -> None:
    """
        This function closes all the tables connections to the sql database.
    """
    oauth_store.close()
    start_url_store.close()
    screenname_store.close()
    userid_store.close()
    worker_id_store.close()
    access_token_store.close()
    access_token_secret_store.close()
    max_page_store.close()
    session_id_store.close()
    twitterversion_store.close()
    mode_store.close()
    participant_id_store.close()
    assignment_id_store.close()
    project_id_store.close()
    completed_survey.close()
    experimental_condition.close()

def delete_single_user_data(user) -> None:
    pass

def filter_tweets(feedtweetsv1,feedtweetsv2):
    print(len(feedtweetsv1))
    print(len(feedtweetsv2))
    level_1_tweets = []
    level_2_retweeted = []
    filtered_feedtweets = []
    filtered_feedtweetsv2 = []
    for (i,tweet) in enumerate(feedtweetsv1):
        unique = False
        no_reply = True
        if tweet["id_str"] not in level_1_tweets:
            if "retweeted_status" in tweet.keys():
                if tweet["retweeted_status"]["id_str"] not in level_1_tweets and tweet["retweeted_status"]["id_str"] not in level_2_retweeted:
                    level_1_tweets.append(tweet["id_str"])
                    level_2_retweeted.append(tweet["retweeted_status"]["id_str"])
                    unique = True
            else:
                level_1_tweets.append(tweet["id_str"])
                unique = True
        if tweet["in_reply_to_status_id_str"]:
            reply = False
        if unique and no_reply:
            filtered_feedtweets.append(tweet)
            filtered_feedtweetsv2.append(feedtweetsv2[i])
    return filtered_feedtweets,filtered_feedtweetsv2

def break_timeline_attention(public_tweets,public_tweets_score,absent_tweets,max_pages):
    db_tweet_payload = []
    db_tweet_attn_payload = []
    absent_tweets_ids = []
    rankk = 0
    tweetids_by_page = defaultdict(list)
    print(absent_tweets)
    all_tweet_ids = [tweet['id'] for tweet in public_tweets if type(tweet) != float]
    for (i,tweet) in enumerate(public_tweets):
        if type(tweet) == float:
                continue
        page = int(rankk/10)
        rank_in_page = (rankk%10) + 1
        db_tweet = {
            'fav_before':str(tweet['favorited']),
            'tid':str(tweet["id"]),
            'rtbefore':str(tweet['retweeted']),
            'page':page,
            'rank':rank_in_page,
            'predicted_score':public_tweets_score[i]
        }
        db_tweet_payload.append(db_tweet)
        tweetids_by_page[page].append(tweet["id"])
        rankk = rankk + 1

    for tweet in absent_tweets:
        if type(tweet) == float:
                continue
        absent_tweets_ids.append(tweet["id_str"])

    for attn_page in range(max_pages):
        present_tweets_ids = tweetids_by_page[attn_page]
        present_tweets_select = np.random.choice(present_tweets_ids,size=3,replace=False)
        absent_tweets_select = np.random.choice(absent_tweets_ids,size=2,replace=False)
        for absent_tweet_id in absent_tweets_select:
            absent_tweets_ids.remove(absent_tweet_id)
        all_attn_tweets = np.concatenate((present_tweets_select,absent_tweets_select),axis=0)
        np.random.shuffle(all_attn_tweets)
        for (attn_rank,tt) in enumerate(all_attn_tweets):
            present = False
            if tt in present_tweets_select:
                present = True
            db_tweet_attn = {
                'tweet_id':str(tt),
                'page':str(attn_page),
                'rank':str(attn_rank),
                'present':present
            }
            db_tweet_attn_payload.append(db_tweet_attn)
    return db_tweet_payload,db_tweet_attn_payload

def createlookups(v2tweetobj,includenext=True,onlyincludequote=False):
    tweet_lookup = {}
    media_lookup = {}
    user_lookup = {}
    next_level_ids = []

    if "data" in v2tweetobj.keys():
        for v2tweet in v2tweetobj["data"]:
            tweet_lookup[v2tweet["id"]] = v2tweet
            if includenext:
                if "referenced_tweets" in v2tweet.keys():
                    for referenced_tweet in v2tweet["referenced_tweets"]: 
                        if referenced_tweet["type"] != "replied_to":
                            if onlyincludequote:
                                if referenced_tweet["type"] == "quoted":
                                    next_level_ids.append(referenced_tweet["id"])
                            else:
                                next_level_ids.append(referenced_tweet["id"])

    if "includes" in v2tweetobj.keys():
        if "media" in v2tweetobj["includes"].keys():
            for media_ele in v2tweetobj["includes"]["media"]:
                media_lookup[media_ele["media_key"]] = media_ele
        if "users" in v2tweetobj["includes"].keys():
            for user_ele in v2tweetobj["includes"]["users"]:
                user_lookup[user_ele["id"]] = user_ele

    return tweet_lookup,media_lookup,user_lookup,next_level_ids

def addallfields(v2tweet,v2user,v2media,v2tweetobj_user=None,v2tweetobj_fav=None):
    v1tweet = {}
    v1tweet["id"] = v2tweet["id"]
    v1tweet["id_str"] = v2tweet["id"]
    v1tweet["full_text"] = v2tweet["text"]
    v1tweet["favorite_count"] = v2tweet["public_metrics"]["like_count"]
    v1tweet["retweet_count"] = v2tweet["public_metrics"]["retweet_count"]
    v1tweet["created_at"] = v2tweet["created_at"]
    v1tweet["in_reply_to_status_id_str"] = ""
    v1tweet["favorited"] = False
    v1tweet["retweeted"] = False

    if "referenced_tweets" in v2tweet.keys():
        for referenced_tweet in v2tweet["referenced_tweets"]:
            if referenced_tweet["type"] == "replied_to":
                v1tweet["in_reply_to_status_id_str"] = referenced_tweet["id"]

    if v2tweetobj_user:
        if "data" in v2tweetobj_user.keys():
            for v2tweet_user in v2tweetobj_user["data"]:
                if "referenced_tweets" in v2tweet_user.keys():
                    for referenced_tweet in v2tweet_user["referenced_tweets"]:
                        if referenced_tweet["type"] == "retweeted":
                            if referenced_tweet["id"] == v2tweet["id"]:
                                v1tweet["retweeted"] = True

    if v2tweetobj_fav:
        if "data" in v2tweetobj_fav.keys():
            for v2tweet_fav in v2tweetobj_fav["data"]:
                if v2tweet_fav["id"] == v2tweet["id"]:
                    v1tweet["favorited"] = True


    v1tweet["user"] = {}
    v1tweet["user"]["name"] = v2user[v2tweet["author_id"]]["name"]
    v1tweet["user"]["profile_image_url"] = v2user[v2tweet["author_id"]]["profile_image_url"]
    v1tweet["user"]["screen_name"] = v2user[v2tweet["author_id"]]["username"]
    v1tweet["user"]["url"] = ""
    if "url" in v2user[v2tweet["author_id"]].keys():
        v1tweet["user"]["url"] = v2user[v2tweet["author_id"]]["url"]

    if "entities" in v2tweet.keys():
        if "urls" in v2tweet["entities"]:
            v1tweet["entities"] = {}
            v1tweet["entities"]["urls"] = []
            for v2_url in v2tweet["entities"]["urls"]:
                v1_url = {}
                v1_url["indices"] = [v2_url["start"],v2_url["end"]]
                v1_url["display_url"] = v2_url["display_url"]
                v1_url["expanded_url"] = v2_url["expanded_url"]
                v1_url["url"] = v2_url["url"]
                v1tweet["entities"]["urls"].append(v1_url)

    if "attachments" in v2tweet.keys():
        if "media_keys" in v2tweet["attachments"].keys():
            if "entities" not in v1tweet.keys():
                v1tweet["entities"] = {}
            v1tweet["entities"]["media"] = []
            for media_key in v2tweet["attachments"]["media_keys"]:
                v1_media = {}
                if "url" in v2media[media_key].keys():
                    v1_media["media_url"] = v2media[media_key]["url"]
                    v1_media["expanded_url"] = v2media[media_key]["url"]
                else:
                    v1_media["media_url"] = v2media[media_key]["preview_image_url"]
                    v1_media["expanded_url"] = v2media[media_key]["preview_image_url"]
                v1tweet["entities"]["media"].append(v1_media)

    return v1tweet


def convertv2tov1(v2tweetobj,cred,v2tweetobj_user=None,v2tweetobj_fav=None):

    oauth_new = OAuth1Session(cred['key'],
                    client_secret=cred['key_secret'],
                    resource_owner_key=cred['token'],
                    resource_owner_secret=cred['token_secret'])

    v1_tweets_all = []

    tweet_1_lookup = {}
    tweet_1_media_lookup = {}
    tweet_1_user_lookup = {}

    tweet_2_lookup = {}
    tweet_2_media_lookup = {}
    tweet_2_user_lookup = {}

    tweet_3_lookup = {}
    tweet_3_media_lookup = {}
    tweet_3_user_lookup = {}

    tweet_2_ids = []
    tweet_3_ids = []

    tweet_1_lookup,tweet_1_media_lookup,tweet_1_user_lookup,tweet_2_ids = createlookups(v2tweetobj)

    if tweet_2_ids:
        new_tweet_params = {
            "ids" : ",".join(tweet_2_ids),
            "tweet.fields" : timeline_params["tweet.fields"],
            "user.fields" : timeline_params["user.fields"],
            "media.fields" : timeline_params["media.fields"],
            "expansions" : timeline_params["expansions"],
        }
        response_tweet_2 = oauth_new.get("https://api.twitter.com/2/tweets", params=new_tweet_params)
        v2tweetobj_2 = json.loads(response_tweet_2.text)
        tweet_2_lookup,tweet_2_media_lookup,tweet_2_user_lookup,tweet_3_ids = createlookups(v2tweetobj_2,onlyincludequote=True)

    if tweet_3_ids:
        new_tweet_params = {
            "ids" : ",".join(tweet_3_ids),
            "tweet.fields" : timeline_params["tweet.fields"],
            "user.fields" : timeline_params["user.fields"],
            "media.fields" : timeline_params["media.fields"],
            "expansions" : timeline_params["expansions"],
        }
        response_tweet_3 = oauth_new.get("https://api.twitter.com/2/tweets", params=new_tweet_params)
        v2tweetobj_3 = json.loads(response_tweet_3.text)
        tweet_3_lookup,tweet_3_media_lookup,tweet_3_user_lookup,no_matter_ids = createlookups(v2tweetobj_3,includenext=False)

    if "data" in v2tweetobj.keys():
        for v2tweet in v2tweetobj["data"]:
            v1tweet = addallfields(v2tweet,tweet_1_user_lookup,tweet_1_media_lookup,v2tweetobj_user=v2tweetobj_user,v2tweetobj_fav=v2tweetobj_fav)
            if "referenced_tweets" in v2tweet.keys():
                for referenced_tweet in v2tweet["referenced_tweets"]:
                    if referenced_tweet["type"] == "retweeted":
                        if referenced_tweet["id"] in tweet_2_lookup.keys():
                            v2tweet_retweeted = tweet_2_lookup[referenced_tweet["id"]]
                            v1tweet["retweeted_status"] = addallfields(v2tweet_retweeted,tweet_2_user_lookup,tweet_2_media_lookup)
                            if "referenced_tweets" in v2tweet_retweeted.keys():
                                for double_referenced_tweet in v2tweet_retweeted["referenced_tweets"]:
                                    if double_referenced_tweet["type"] == "quoted":
                                        if double_referenced_tweet["id"] in tweet_3_lookup.keys():
                                            v2tweet_retweeted_quoted = tweet_3_lookup[double_referenced_tweet["id"]]
                                            v1tweet["retweeted_status"]["quoted_status"] = addallfields(v2tweet_retweeted_quoted,tweet_3_user_lookup,tweet_3_media_lookup)
                    if referenced_tweet["type"] == "quoted":
                        if referenced_tweet["id"] in tweet_2_lookup.keys():
                            v2tweet_quoted = tweet_2_lookup[referenced_tweet["id"]]
                            v1tweet["quoted_status"] = addallfields(v2tweet_quoted,tweet_2_user_lookup,tweet_2_media_lookup)
            v1_tweets_all.append(v1tweet)

    return v1_tweets_all


@app.route('/auth/')
def start():
    cred = config('../configuration/config.ini','twitterapp')

    try:
        request_token = OAuth1Session(client_key=cred['key'],client_secret=cred['key_secret'])
        content = request_token.post(request_token_url, data = {"oauth_callback":app_callback_url})
        logging.info('Twitter access successfull')
    except Exception as error:
        print('Twitter access failed with error : '+str(error))
        logging.error('Twitter access failed with error : '+str(error))

    #request_token = dict(urllib.parse.parse_qsl(content))
    #oauth_token = request_token[b'oauth_token'].decode('utf-8')
    #oauth_token_secret = request_token[b'oauth_token_secret'].decode('utf-8')

    data_tokens = content.text.split("&")

    oauth_token = data_tokens[0].split("=")[1]
    oauth_token_secret = data_tokens[1].split("=")[1]
    oauth_store[oauth_token] = oauth_token_secret
    start_url = authorize_url+"?oauth_token="+oauth_token
    #res = make_response(render_template('index.html', authorize_url=authorize_url, oauth_token=oauth_token, request_token_url=request_token_url))
    res = make_response(render_template('YouGov.html', start_url=start_url, screenname="###", rockwell_url="###"))
    # Trying to add a browser cookie
    #res.set_cookie('exp','infodiversity',max_age=1800)
    return res
    #return render_template('index.html', authorize_url=authorize_url, oauth_token=oauth_token, request_token_url=request_token_url)

@app.route('/qualauth/')
def qualstart():
    print("Qualstart called!")
    for key in request.args:
        print(key)
    cred = config('../configuration/config.ini','twitterapp')
    try:
        request_token = OAuth1Session(client_key=cred['key'],client_secret=cred['key_secret'])
        content = request_token.post(request_token_url, data = {"oauth_callback":app_callback_url_qual})
        logging.info('Twitter access successfull')
    except Exception as error:
        print('Twitter access failed with error : '+str(error))
        logging.error('Twitter access failed with error : '+str(error))

    #request_token = dict(urllib.parse.parse_qsl(content))
    #oauth_token = request_token[b'oauth_token'].decode('utf-8')
    #oauth_token_secret = request_token[b'oauth_token_secret'].decode('utf-8')

    data_tokens = content.text.split("&")

    oauth_token = data_tokens[0].split("=")[1]
    oauth_token_secret = data_tokens[1].split("=")[1] 
    oauth_store[oauth_token] = oauth_token_secret
    print("OAuth token from qualauth")
    print(oauth_token)
    screenname_store[oauth_token] = "####"
    start_url = authorize_url+"?oauth_token="+oauth_token
    start_url_store[oauth_token] = start_url
    #res = make_response(render_template('YouGovQualtrics.html', start="Yes", start_url=start_url))
    #return res
    return oauth_token
    #res = make_response(render_template('index.html', authorize_url=authorize_url, oauth_token=oauth_token, request_token_url=request_token_url))
    #res = make_response(render_template('YouGov.html', start_url=start_url, screenname="###", rockwell_url="###"))
    # Trying to add a browser cookie
    #res.set_cookie('exp','infodiversity',max_age=1800)
    #return res
    #return render_template('index.html', authorize_url=authorize_url, oauth_token=oauth_token, request_token_url=request_token_url)

@app.route('/logfrontenderrors')
def logerrors():
    message = request.args.get('message')
    stack = request.args.get('message')
    worker_id = request.args.get('worker_id')
    ip_address = request.environ['REMOTE_ADDR']
    logging.info(f"Error in the frontend : {worker_id=} {ip_address=} {message=} {stack=}")
    return "Done!"

@app.route('/qualrender')
def qualrender():
    print("Qualrender called!")
    for key in request.args:
        print(key)
        print(request.args.get(key))
    oauth_token_qualtrics = request.args.get('oauth_token')
    mode = request.args.get('mode').strip()
    participant_id = request.args.get('participant_id').strip()
    mode_store[oauth_token_qualtrics] = mode
    participant_id_store[oauth_token_qualtrics] = participant_id
    assignment_id_store[oauth_token_qualtrics] = "NA"
    project_id_store[oauth_token_qualtrics] = "NA"
    start_url = start_url_store[oauth_token_qualtrics]
    res = make_response(render_template('YouGovQualtrics.html', start="Yes", start_url=start_url, oauth_token=oauth_token_qualtrics, mode=mode ,secretidentifier="_rockwellidentifierv2_", insertfeedurl=webInformation['url']+"/insertfeedqualtrics", setscreenname=webInformation['url']+"/auth/setscreenname"))
    return res

@app.route('/qualcallback')
def qualcallback():
    print("CALLBACK CALLED!!!!")
    oauth_token = request.args.get('oauth_token')
    oauth_verifier = request.args.get('oauth_verifier')
    oauth_denied = request.args.get('denied')


    if oauth_denied:
        if oauth_denied in oauth_store:
            del oauth_store[oauth_denied]
        #screenname_store[oauth_token] = "#DENIED#"
        #return render_template('error.html', error_message="the OAuth request was denied by this user")
        #return redirect('http://' + str(webInformation['url']) + ':5000')
        return "<script>window.onload = window.close();</script>"

    #if not oauth_token or not oauth_verifier:
    #    return render_template('error.html', error_message="callback param(s) missing")

    # unless oauth_token is still stored locally, return error
    #if oauth_token not in oauth_store:
    #    return render_template('error.html', error_message="oauth_token not found locally")

    oauth_token_secret = oauth_store[oauth_token]

    # if we got this far, we have both callback params and we have
    # found this token locally

    #consumer = oauth.Consumer(
    #    app.config['APP_CONSUMER_KEY'], app.config['APP_CONSUMER_SECRET'])
    #token = oauth.Token(oauth_token, oauth_token_secret)
    #token.set_verifier(oauth_verifier)
    #client = oauth.Client(consumer, token)

    #resp, content = client.request(access_token_url, "POST")
    
    cred = config('../configuration/config.ini','twitterapp')
    oauth_access_tokens = OAuth1Session(client_key=cred['key'],client_secret=cred['key_secret'],resource_owner_key=oauth_token,resource_owner_secret=oauth_token_secret,verifier=oauth_verifier)
    content = oauth_access_tokens.post(access_token_url)  

    #access_token = dict(urllib.parse.parse_qsl(content))

    access_token = content.text.split("&")

    # These are the tokens you would store long term, someplace safe
    real_oauth_token = access_token[0].split("=")[1]
    real_oauth_token_secret = access_token[1].split("=")[1]
    user_id = access_token[2].split("=")[1]
    screen_name = access_token[3].split("=")[1]

    oauth_account_settings = OAuth1Session(client_key=cred['key'],client_secret=cred['key_secret'],resource_owner_key=real_oauth_token,resource_owner_secret=real_oauth_token_secret)
    response = oauth_account_settings.get(account_settings_url)
    account_settings_user = json.dumps(json.loads(response.text))
    response_creation_date = oauth_account_settings.get(creation_date_url,params={'ids':[user_id]})
    creation_date_user = json.dumps(json.loads(response_creation_date.text))

    mode = mode_store[oauth_token]
    mturk_ref_id = 1

    if mode == "ELIGIBILITY":
        print("IN ELIGIBILITY")
        participant_id = participant_id_store[oauth_token]
        print(participant_id)
        assignment_id = assignment_id_store[oauth_token]
        project_id = project_id_store[oauth_token]
        db_response_worker_id = requests.get('http://127.0.0.1:5052/get_existing_user_by_twitter_id?twitter_id='+str(user_id))
        if db_response_worker_id.json()['data'] != "NEW":
            worker_id = db_response_worker_id.json()['data'][0][0].strip()
        else:
            worker_id = ''.join(random.choice(string.ascii_uppercase + string.ascii_lowercase + string.digits) for _ in range(10))
            insert_user_payload = {'worker_id' : worker_id, 'twitter_id': str(user_id),'access_token': real_oauth_token, 'access_token_secret': real_oauth_token_secret, 'screenname': screen_name, 'account_settings': account_settings_user, 'creation_date':creation_date_user, 'oauth_token':oauth_token, 'participant_id':participant_id}
            requests.get('http://' + webInformation['localhost'] + ':5052/insert_user',params=insert_user_payload)
        screenname_store[oauth_token] = screen_name
        userid_store[oauth_token] = user_id
        worker_id_store[oauth_token] = str(worker_id)
        access_token_store[oauth_token] = real_oauth_token
        access_token_secret_store[oauth_token] = real_oauth_token_secret
        hometimeline_pulled_store[worker_id] = False
        completed_survey[worker_id] = False

    else:
        worker_id = '#NOTEXIST#'
        db_response_screenname = requests.get('http://127.0.0.1:5052/get_existing_tweets_new_screenname?screenname='+str(screen_name)+"&page="+str(0)+"&feedtype=S")
        if db_response_screenname.json()['data'] != "NEW":
            worker_id = db_response_screenname.json()['data'][0][-2].strip()

    del oauth_store[oauth_token]

    res = make_response(render_template('YouGovQualtrics.html', start="No", worker_id=worker_id, oauth_token=oauth_token, mode=mode ,secretidentifier="_rockwellidentifierv2_", insertfeedurl=webInformation['url']+"/insertfeedqualtrics", setscreenname=webInformation['url']+"/auth/setscreenname"))
    return res

    #return "<script>window.onload = window.close();</script>"
    #return "Done!!"
    
    #insert_user_payload = {'twitter_id': str(user_id), 'account_settings': account_settings_user}
    #resp_worker_id = requests.get('http://' + webInformation['url'] + ':5052/insert_user',params=insert_user_payload)
    #worker_id = resp_worker_id.json()["data"]

    #attn = 0
    #page = 0
    #pre_attn_check = 1

    #rockwell_url_agg = str(webInformation['app_route']) + '?access_token=' + str(real_oauth_token) + '&access_token_secret=' + str(real_oauth_token_secret) + '&worker_id=' + str(worker_id) + '&attn=' + str(attn) + '&page=' + str(page) 
    #rockwell_url_agg = 'http://127.0.0.1:3000' + '?access_token=' + str(real_oauth_token) + '&access_token_secret=' + str(real_oauth_token_secret) + '&worker_id=' + str(worker_id) + '&attn=' + str(attn) + '&page=' + str(page) + '&pre_attn_check=' + str(pre_attn_check)

    #redirect(rockwell_url + '?access_token=' + real_oauth_token + '&access_token_secret=' + real_oauth_token_secret)

    #return render_template('placeholder.html', worker_id=worker_id, access_token=real_oauth_token, access_token_secret=real_oauth_token_secret)
    #return render_template('YouGov.html', start_url="###", screenname=screen_name, rockwell_url=rockwell_url_agg)

@app.route('/insertfeedqualtrics', methods=['GET','POST'])
def insert_feed_qualtrics():
    worker_id = request.args.get('worker_id').strip()
    print("Worker ID in insertfeedqualtrics")
    print(worker_id)
    if worker_id == '#NOTEXIST#':
        return worker_id+"$$$"+"False"
    oauth_token = request.args.get('oauth_token')
    db_response = requests.get('http://127.0.0.1:5052/get_existing_user?worker_id='+str(worker_id))
    if len(db_response.json()['data']) == 0:
        return "NA"+"$$$"+"False"
    db_response = db_response.json()['data']
    access_token = db_response[0][0]
    access_token_secret = db_response[0][1]
    screenname = db_response[0][2]
    userid = db_response[0][3]
    print("Screenname in insertfeedqualtrics")
    print(screenname)
    db_response = requests.get('http://127.0.0.1:5052/get_existing_tweets_new?worker_id='+str(worker_id)+"&page="+str(0)+"&feedtype=S")
    need_to_fetch_screenname = False
    need_to_fetch_tweets = False
    if db_response.json()['data'] == "NEW":
        need_to_fetch_screenname = True
    if need_to_fetch_screenname:
        db_response_screenname = requests.get('http://127.0.0.1:5052/get_existing_tweets_new_screenname_screen_2?screenname='+str(screenname)+"&page="+str(0)+"&feedtype=S")
        if db_response_screenname.json()['data'] == "NEW":
            need_to_fetch_tweets = True
        else:
            worker_id = db_response_screenname.json()['data'][0][-2].strip()
    screenname_exists_return = "True"
    if need_to_fetch_tweets:
        screenname_exists_return = "False"
    print("Worker ID in insertfeedqualtrics 2")
    print(worker_id)
    return worker_id+"$$$"+screenname_exists_return

@app.route('/auth/setscreenname',methods=['GET','POST'])
def set_screenname():
    oauth_token = request.args.get('oauth_token')
    worker_id = request.args.get('worker_id')
    print(worker_id)
    screenname_exists = request.args.get('screenname_exists')
    print("IN SET SCREENNAME:::")
    print(screenname_exists)
    db_response = requests.get('http://127.0.0.1:5052/get_existing_user?worker_id='+str(worker_id))
    db_response = db_response.json()['data']
    access_token = db_response[0][0]
    access_token_secret = db_response[0][1]
    screenname = db_response[0][2]
    userid = db_response[0][3]
    experimental_condition_ret = db_response[0][4].strip()
    print("EXPERIMENTAL CONDITION")
    print(experimental_condition_ret)
    if screenname_exists == "False":
        screenname_store[oauth_token] = "#NOTEXIST#"
    else:
        screenname_store[oauth_token] = screenname
    userid_store[oauth_token] = userid
    worker_id_store[oauth_token] = str(worker_id)
    access_token_store[oauth_token] = access_token
    access_token_secret_store[oauth_token] = access_token_secret
    hometimeline_pulled_store[worker_id] = False
    completed_survey[worker_id] = False
    experimental_condition[worker_id] = experimental_condition_ret
    return "Done!"

@app.route('/auth/getscreenname',methods=['GET','POST'])
def screenname():
    #print("GET SCEEN NAME CALLED!!!")
    oauth_token_qualtrics = request.args.get('oauth_token')
    screen_name_return = screenname_store[oauth_token_qualtrics]
    print("SCREEN NAME CALLED!!!!!!!!")
    print(screen_name_return)
    if screen_name_return == "####":
        return screen_name_return
    if screen_name_return == '#NOTEXIST#':
        return screen_name_return
    random_identifier_len = random.randint(15, 26)    
    random_identifier = ''.join(random.choice(string.ascii_uppercase + string.ascii_lowercase + string.digits) for _ in range(random_identifier_len))
    userid_return = userid_store[oauth_token_qualtrics]
    worker_id_return = worker_id_store[oauth_token_qualtrics]
    access_token_return = access_token_store[oauth_token_qualtrics]
    access_token_secret_return = access_token_secret_store[oauth_token_qualtrics]
    file_number = 1
    existing_home_timeline_files = sorted(glob.glob("UserData/{}_home_*.json.gz".format(userid_return)))
    if existing_home_timeline_files:
        latest_user_file = max(existing_home_timeline_files, key=lambda fn: int(fn.split(".")[0].split("_")[2]))
        file_number = int(latest_user_file.split(".")[0].split("_")[2]) + 1
    return screen_name_return+"$$$"+str(userid_return)+"$$$"+worker_id_return+"$$$"+access_token_return+"$$$"+access_token_secret_return+"$$$"+random_identifier+"$$$"+str(file_number)  

@app.route('/retweet_post', methods=['GET','POST'])
def retweet_post():
    worker_id = request.args.get('worker_id').strip()
    tweet_id = request.args.get('tweet_id').strip()
    db_response = requests.get('http://127.0.0.1:5052/get_existing_user?worker_id='+str(worker_id))
    db_response = db_response.json()['data']
    access_token = db_response[0][0]
    access_token_secret = db_response[0][1]
    userid = db_response[0][3]

    logging.info(f"Retweet request started for : {userid=}")

    ratelimiter.push_retweet(tweet_id,userid,access_token,access_token_secret)
    return jsonify({"success":1}) # Retweet successful

    """

    cred = config('../configuration/config.ini','twitterapp')
    cred['token'] = access_token.strip()
    cred['token_secret'] = access_token_secret.strip()
    oauth = OAuth1Session(cred['key'],
                        client_secret=cred['key_secret'],
                        resource_owner_key=cred['token'],
                        resource_owner_secret=cred['token_secret'])

    try:
        payload = {"tweet_id" : tweet_id}
        response_retweet = oauth.post("https://api.twitter.com/2/users/{}/retweets".format(userid), json=payload)
        print(response_retweet.text)
        response_text = response_retweet.text
        logging.info(f"Retweet returned from Twitter : {userid=} {response_text=}")
        return jsonify({"success":1}) # Retweet successful
    except Exception as e:
        print("Retweet Exception")
        print(e)
        error = e
        logging.info(f"Retweet exception : {userid=} {error=}")
        return jsonify({"success":0}) # Retweet failed
    """

@app.route('/like_post', methods=['GET','POST'])
def like_post():
    worker_id = request.args.get('worker_id').strip()  
    tweet_id = request.args.get('tweet_id').strip()  
    db_response = requests.get('http://127.0.0.1:5052/get_existing_user?worker_id='+str(worker_id))
    db_response = db_response.json()['data']
    access_token = db_response[0][0]
    access_token_secret = db_response[0][1]
    userid = db_response[0][3]

    logging.info(f"Like request started for : {userid=}")

    ratelimiter.push_like(tweet_id,userid,access_token,access_token_secret)
    return jsonify({"success":1}) # Like successful

    """
    cred = config('../configuration/config.ini','twitterapp')
    cred['token'] = access_token.strip()
    cred['token_secret'] = access_token_secret.strip()
    oauth = OAuth1Session(cred['key'],
                        client_secret=cred['key_secret'],
                        resource_owner_key=cred['token'],
                        resource_owner_secret=cred['token_secret'])

    try:
        payload = {"tweet_id" : tweet_id}
        response_likes = oauth.post("https://api.twitter.com/2/users/{}/likes".format(userid), json=payload)
        print(response_likes.json().keys())
        response_text = response_likes.text
        print(response_text)
        print(time.time())
        print(response_likes.headers['x-rate-limit-reset'])
        logging.info(f"Likes returned from Twitter : {userid=} {response_text=}")
        return jsonify({"success":1}) # Retweet successful
    except Exception as e:
        print(e)
        error = e
        logging.info(f"Likes exception : {userid=} {error=}")
        return jsonify({"success":0}) # Retweet failed
    """

@app.route('/hometimeline_from_file', methods=['GET'])
def get_hometimeline_from_file():
    worker_id = request.args.get('worker_id').strip()
    db_response = requests.get('http://127.0.0.1:5052/get_existing_user?worker_id='+str(worker_id))
    db_response = db_response.json()['data']
    access_token = db_response[0][0]
    access_token_secret = db_response[0][1]
    screenname = db_response[0][2]
    userid = db_response[0][3]
    file_number = '1'
    with gzip.open("UserDatav2/{}_home_{}.json.gz".format(userid,file_number)) as ff:
        data = json.loads(ff.read().decode('utf-8'))
    v2tweetobj_arr = data['homeTweets']
    cred = config('../configuration/config.ini','twitterapp')
    cred['token'] = access_token.strip()
    cred['token_secret'] = access_token_secret.strip()
    oauth = OAuth1Session(cred['key'],client_secret=cred['key_secret'],resource_owner_key=cred['token'],resource_owner_secret=cred['token_secret'])
    v1tweetobj_arr = []
    db_tweet_payload = []
    feed_tweets = []
    feed_tweets_v2 = []
    for v2tweetobj in v2tweetobj_arr:
        v1tweetobj = convertv2tov1(v2tweetobj,cred)
        v1tweetobj_arr.append(v1tweetobj)
        feed_tweets_itr,feed_tweets_v2_itr = filter_tweets(v1tweetobj,v2tweetobj["data"])
        feed_tweets.extend(feed_tweets_itr)
        feed_tweets_v2.extend(feed_tweets_v2_itr)
        for (i,tweet) in enumerate(feed_tweets_itr):
            db_tweet = {'tweet_id':tweet["id"],'tweet_json':tweet, 'tweet_json_v2':feed_tweets_v2[i]}
            db_tweet_payload.append(db_tweet)
    db_response = requests.get('http://127.0.0.1:5052/get_existing_tweets_all?worker_id='+str(worker_id))
    if db_response.json()['data'] != "NEW":
        existing_tweets = [response[7] for response in db_response.json()['data']]
        feed_tweets.extend(existing_tweets)
    absent_tweets = feed_tweets[-10:]
    feed_tweets_chronological = []
    feed_tweets_chronological_score = []
    for tweet in feed_tweets:
        feed_tweets_chronological.append(tweet)
        feed_tweets_chronological_score.append(-100)
    db_tweet_chronological_payload = []
    db_tweet_chronological_attn_payload = []
    rankk = 0
    tweet_ids_inserted = []
    num_duplicates = 0
    for (i,tweet) in enumerate(feed_tweets_chronological):
        if type(tweet) == float:
            continue
        if str(tweet["id"]) in tweet_ids_inserted:
            num_duplicates = num_duplicates + 1
            continue
        tweet_ids_inserted.append(str(tweet["id"]))
        page = int(rankk/10)
        rank_in_page = (rankk%10) + 1
        db_tweet = {'fav_before':str(tweet['favorited']),'tid':str(tweet["id"]),'rtbefore':str(tweet['retweeted']),'page':page,'rank':rank_in_page,'predicted_score':feed_tweets_chronological_score[i]}
        db_tweet_chronological_payload.append(db_tweet)
        rankk = rankk + 1
    print("Duplicates : "+str(num_duplicates))
    finalJson = []
    finalJson.append(db_tweet_payload)
    finalJson.append(db_tweet_chronological_payload)
    finalJson.append(db_tweet_chronological_attn_payload)
    finalJson.append(worker_id)
    finalJson.append(screenname)
    requests.post('http://127.0.0.1:5052/insert_timelines_attention_chronological',json=finalJson)
    return "Done!"

@app.route('/hometimeline', methods=['GET'])
def get_hometimeline():
    #logger = make_logger(LOG_PATH_DEFAULT)
    print("Here!!!!")
    worker_id = request.args.get('worker_id').strip()
    if worker_id in hometimeline_pulled_store.keys():
        if hometimeline_pulled_store[worker_id]:
            print("Yahan???")
            return "Done!"
        else:
            hometimeline_pulled_store[worker_id] = True
    logging.info(f"Hometimeline endpoint started for : {worker_id=}")
    file_number = request.args.get('file_number').strip()
    max_id = request.args.get('max_id').strip()
    collection_started = request.args.get('collection_started').strip()
    num_tweets_cap = 1
    num_itr = int(HOMETIMELINE_CAP/TIMELINE_CAP)
    db_response = requests.get('http://127.0.0.1:5052/get_existing_user?worker_id='+str(worker_id))
    db_response = db_response.json()['data']
    access_token = db_response[0][0]
    access_token_secret = db_response[0][1]
    screenname = db_response[0][2]
    userid = db_response[0][3]
    participant_id = "NA"
    assignment_id = "NA"
    project_id = "NA"
    logging.info(f"Participant information : {worker_id=} {screenname=} {userid=} {participant_id=} {assignment_id=} {project_id=}")
    logging.info(f"Twitter information : {worker_id=} {access_token=} {access_token_secret=}")
    v2tweetobj = {}
    v1tweetobj = {}

    errormessage = "NA"

    cred = config('../configuration/config.ini','twitterapp')
    cred['token'] = access_token.strip()
    cred['token_secret'] = access_token_secret.strip()
    oauth = OAuth1Session(cred['key'],
                        client_secret=cred['key_secret'],
                        resource_owner_key=cred['token'],
                        resource_owner_secret=cred['token_secret'])
    logging.info(f"Create OAuth session : {worker_id=}")
    timeline_params_cap = {}
    for kk in timeline_params:
        timeline_params_cap[kk] = timeline_params[kk]
    timeline_params_cap["max_results"] = TIMELINE_CAP
    v2tweetobj_arr = []
    v1tweetobj_arr = []
    db_tweet_payload = []
    feed_tweets = []
    feed_tweets_v2 = []
    pagination_token = ""
    for i in range(num_itr):
        if pagination_token:
            timeline_params_cap["pagination_token"] = pagination_token

        response = oauth.get("https://api.twitter.com/2/users/{}/timelines/reverse_chronological".format(userid), params = timeline_params_cap)
        logging.info(f"Got response from Twitter API : {worker_id=}")

        if response.text == '{"errors":[{"code":89,"message":"Invalid or expired token."}]}':
            errormessage = "Invalid Token"
            logging.info(f"Invalid Token error : {worker_id=}")
            pagination_token = ""

        if response.text == "{'errors': [{'message': 'Rate limit exceeded', 'code': 88}]}":
            errormessage = "Rate Limit Exceeded"
            logging.info(f"Rate limit exceeded error : {worker_id=}")
            pagination_token = ""

        if errormessage == "NA":

            v2tweetobj_loaded = json.loads(response.text)
            
            if "meta" in v2tweetobj_loaded.keys():
                if "next_token" in v2tweetobj_loaded["meta"].keys():
                    pagination_token = v2tweetobj_loaded["meta"]["next_token"]
                    print(pagination_token)
                else:
                    pagination_token = ""
            else:
                pagination_token = ""

            if max_id != "INITIAL":
                for section in v2tweetobj_loaded.keys():
                    if section == "data":
                        v2tweetobj["data"] = []
                        for v2tweet in v2tweetobj_loaded["data"]:
                            if int(v2tweet["id"]) > int(max_id):
                                v2tweetobj["data"].append(v2tweet)
                    else:
                        v2tweetobj[section] = v2tweetobj_loaded[section]
            else:
                v2tweetobj = v2tweetobj_loaded

            v2tweetobj_arr.append(v2tweetobj)

            v1tweetobj = convertv2tov1(v2tweetobj,cred)
            print("Length of Tweets Collected : "+str(len(v1tweetobj)))
            v1tweetobj_arr.append(v1tweetobj)
            logging.info(f"Converted Version 2 Twitter JSON object to Version 1 : {worker_id=}")
        
        else:
            pagination_token = ""

        if "data" in v2tweetobj.keys():
            feed_tweets_itr,feed_tweets_v2_itr = filter_tweets(v1tweetobj,v2tweetobj["data"])
            feed_tweets.extend(feed_tweets_itr)
            feed_tweets_v2.extend(feed_tweets_v2_itr)
            for (i,tweet) in enumerate(feed_tweets_itr):
                db_tweet = {'tweet_id':tweet["id"],'tweet_json':tweet, 'tweet_json_v2':feed_tweets_v2[i]}
                db_tweet_payload.append(db_tweet)

        if not pagination_token:
            break

    now_session_start = datetime.datetime.now()
    session_start = now_session_start.strftime('%Y-%m-%dT%H:%M:%S')

    collection_started_store = collection_started
    if collection_started == "INITIAL":
        collection_started_store = session_start

    newest_id = ""
    if "meta" in v2tweetobj.keys():
        if "newest_id" in v2tweetobj["meta"]:
            newest_id = v2tweetobj["meta"]["newest_id"]

    userobj = {
        "screen_name" : screenname,
        "twitter_id" : userid
    }

    queries = compose_queries_512_chars(screenname)
    user_eng_queries = []
    for qq in queries:
        user_eng_queries.append({'query':qq,'since_id':'0','next_token':'##START##'})

    logging.info(f"Generated queries for pulling engagement data : {worker_id=}")

    writeObj = {
        "MTurkId" : participant_id,
        "MTurkHitId" : assignment_id,
        "MTurkAssignmentId" : project_id,
        "collectionStarted" : collection_started_store,
        "timestamp" : session_start,
        "source": "pilot3",
        "accessToken": access_token,
        "accessTokenSecret": access_token_secret,
        "latestTweetId": newest_id,
        "worker_id": worker_id,
        "userObject": userobj,
        "homeTweets": v1tweetobj_arr,
        "errorMessage" : errormessage
    }

    writeObjv2 = {
        "MTurkId" : participant_id,
        "MTurkHitId" : assignment_id,
        "MTurkAssignmentId" : project_id,
        "collectionStarted" : collection_started_store,
        "timestamp" : session_start,
        "source": "pilot3",
        "accessToken": access_token,
        "accessTokenSecret": access_token_secret,
        "latestTweetId": newest_id,
        "worker_id": worker_id,
        "userObject": userobj,
        "homeTweets" : v2tweetobj_arr,
        "errorMessage" : errormessage
    }

    logging.info(f"Created object for writing to file : {worker_id=}")

    with gzip.open("hometimeline_data/{}_home_{}.json.gz".format(userid,file_number),"w") as outfile:
        outfile.write(json.dumps(writeObj).encode('utf-8'))

    with gzip.open("UserDatav2/{}_home_{}.json.gz".format(userid,file_number),"w") as outfile:
        outfile.write(json.dumps(writeObjv2).encode('utf-8'))

    logging.info(f"Wrote object to file : {worker_id=}")

    if db_tweet_payload:
        logging.info(f"Started steps for database insertion : {worker_id=}")
        db_response = requests.get('http://127.0.0.1:5052/get_existing_tweets_all?worker_id='+str(worker_id))
        if db_response.json()['data'] != "NEW":
            existing_tweets = [response[7] for response in db_response.json()['data']]
            feed_tweets.extend(existing_tweets)
        logging.info(f"Got existing tweets : {worker_id=}")
        absent_tweets = feed_tweets[-10:]
        feed_tweets_chronological = []
        feed_tweets_chronological_score = []
        for tweet in feed_tweets:
            feed_tweets_chronological.append(tweet)
            feed_tweets_chronological_score.append(-100)
        db_tweet_chronological_payload = []
        db_tweet_chronological_attn_payload = []
        rankk = 0
        tweet_ids_inserted = []
        duplicates = 0
        for (i,tweet) in enumerate(feed_tweets_chronological):
            if type(tweet) == float:
                continue
            if str(tweet["id"]) in tweet_ids_inserted:
                duplicates = duplicates + 1
                continue
            tweet_ids_inserted.append(str(tweet["id"]))
            page = int(rankk/10)
            rank_in_page = (rankk%10) + 1
            db_tweet = {
                'fav_before':str(tweet['favorited']),
                'tid':str(tweet["id"]),
                'rtbefore':str(tweet['retweeted']),
                'page':page,
                'rank':rank_in_page,
                'predicted_score':feed_tweets_chronological_score[i]
            }
            db_tweet_chronological_payload.append(db_tweet)
            rankk = rankk + 1
        print("Duplicates : ")
        print(duplicates)
        finalJson = []
        finalJson.append(db_tweet_payload)
        finalJson.append(db_tweet_chronological_payload)
        finalJson.append(db_tweet_chronological_attn_payload)
        finalJson.append(worker_id)
        finalJson.append(screenname)
        logging.info(f"Created finalJSON for insertion : {worker_id=}")
        requests.post('http://127.0.0.1:5052/insert_timelines_attention_chronological',json=finalJson)
        logging.info(f"Completed database insertion : {worker_id=}")
    else:
        errormessage = errormessage + " No data in v2 tweet object"

    logging.info(f"Trying user timeline : {worker_id=}")
    num_itr = int(USERTIMELINE_CAP/TIMELINE_CAP)
    v2tweetobj_arr = []
    v1tweetobj_arr = []
    timeline_params_cap = {}
    for kk in timeline_params:
        timeline_params_cap[kk] = timeline_params[kk]
    timeline_params_cap["max_results"] = TIMELINE_CAP
    pagination_token = ""
    for i in range(num_itr):
        print("CALLINE USERTIMELINE")
        print(i)
        if pagination_token:
            timeline_params_cap["pagination_token"] = pagination_token
        response = oauth.get("https://api.twitter.com/2/users/{}/tweets".format(userid), params = timeline_params_cap)
        if response.text == '{"errors":[{"code":89,"message":"Invalid or expired token."}]}':
            errormessage = "Invalid Token User timeline"

        if response.text == "{'errors': [{'message': 'Rate limit exceeded', 'code': 88}]}":
            errormessage = "Rate Limit Exceeded User timeline"

        if errormessage == "NA":
            v2tweetobj = json.loads(response.text)
            if "meta" in v2tweetobj.keys():
                if "next_token" in v2tweetobj["meta"].keys():
                    pagination_token = v2tweetobj["meta"]["next_token"]
                    print(pagination_token)
                else:
                    pagination_token = ""
            else:
                pagination_token = ""
            v2tweetobj_arr.append(v2tweetobj)
            v1tweetobj = convertv2tov1(v2tweetobj,cred)
            print(len(v1tweetobj))
            v1tweetobj_arr.append(v1tweetobj)
        else:
            pagination_token = ""
        if not pagination_token:
            break

    newest_id = ""
    if "meta" in v2tweetobj.keys():
        if "newest_id" in v2tweetobj["meta"].keys():
            newest_id = v2tweetobj["meta"]["newest_id"]

    userobj = {
        "screen_name" : screenname,
        "twitter_id" : userid
    }

    now_session_start = datetime.datetime.now()
    session_start = now_session_start.strftime('%Y-%m-%dT%H:%M:%S')

    writeObj = {
        "MTurkId" : participant_id,
        "MTurkHitId" : assignment_id,
        "MTurkAssignmentId" : project_id,
        "timestamp" : session_start,
        "source": "pilot3",
        "accessToken": access_token,
        "accessTokenSecret": access_token_secret,
        "latestTweetId": newest_id,
        "worker_id": worker_id,
        "userObject": userobj,
        "userTweets" : v1tweetobj_arr,
        "errorMessage" : errormessage
    }

    writeObjv2 = {
        "MTurkId" : participant_id,
        "MTurkHitId" : assignment_id,
        "MTurkAssignmentId" : project_id,
        "timestamp" : session_start,
        "source": "pilot3",
        "accessToken": access_token,
        "accessTokenSecret": access_token_secret,
        "latestTweetId": newest_id,
        "worker_id": worker_id,
        "userObject": userobj,
        "userTweets" : v2tweetobj_arr,
        "errorMessage" : errormessage
    }

    with gzip.open("usertimeline_data/{}_user.json.gz".format(userid),"w") as outfile:
        outfile.write(json.dumps(writeObj).encode('utf-8'))

    with gzip.open("UserDatav2/{}_user.json.gz".format(userid),"w") as outfile:
        outfile.write(json.dumps(writeObj).encode('utf-8'))

    logging.info(f"Trying favorites : {worker_id=}")
    num_itr = int(FAVORITES_CAP/TIMELINE_CAP)
    v2tweetobj_arr = []
    v1tweetobj_arr = []
    timeline_params_cap = {}
    for kk in timeline_params:
        timeline_params_cap[kk] = timeline_params[kk]
    timeline_params_cap["max_results"] = TIMELINE_CAP
    pagination_token = ""
    for i in range(num_itr):
        print("CALLINE FAVORITE")
        print(i)
        if pagination_token:
            timeline_params_cap["pagination_token"] = pagination_token
        response = oauth.get("https://api.twitter.com/2/users/{}/liked_tweets".format(userid), params = timeline_params_cap)
        if response.text == '{"errors":[{"code":89,"message":"Invalid or expired token."}]}':
            errormessage = "Invalid Token User timeline"

        if response.text == "{'errors': [{'message': 'Rate limit exceeded', 'code': 88}]}":
            errormessage = "Rate Limit Exceeded User timeline"

        if errormessage == "NA":
            v2tweetobj = json.loads(response.text)
            if "meta" in v2tweetobj.keys():
                if "next_token" in v2tweetobj["meta"].keys():
                    pagination_token = v2tweetobj["meta"]["next_token"]
                    print(pagination_token)
                else:
                    pagination_token = ""
            else:
                pagination_token = ""
            v2tweetobj_arr.append(v2tweetobj)
            v1tweetobj = convertv2tov1(v2tweetobj,cred)
            print(len(v1tweetobj))
            v1tweetobj_arr.append(v1tweetobj)
        else:
            pagination_token = ""
        if not pagination_token:
            break

    newest_id = ""
    if "meta" in v2tweetobj.keys():
        if "newest_id" in v2tweetobj["meta"].keys():
            newest_id = v2tweetobj["meta"]["newest_id"]

    userobj = {
        "screen_name" : screenname,
        "twitter_id" : userid
    }

    now_session_start = datetime.datetime.now()
    session_start = now_session_start.strftime('%Y-%m-%dT%H:%M:%S')

    writeObj = {
        "MTurkId" : participant_id,
        "MTurkHitId" : assignment_id,
        "MTurkAssignmentId" : project_id,
        "timestamp" : session_start,
        "source": "pilot3",
        "accessToken": access_token,
        "accessTokenSecret": access_token_secret,
        "latestTweetId": newest_id,
        "worker_id": worker_id,
        "userObject": userobj,
        "likedTweets" : v1tweetobj_arr,
        "errorMessage" : errormessage
    }

    writeObjv2 = {
        "MTurkId" : participant_id,
        "MTurkHitId" : assignment_id,
        "MTurkAssignmentId" : project_id,
        "timestamp" : session_start,
        "source": "pilot3",
        "accessToken": access_token,
        "accessTokenSecret": access_token_secret,
        "latestTweetId": newest_id,
        "worker_id": worker_id,
        "userObject": userobj,
        "likedTweets" : v2tweetobj_arr,
        "errorMessage" : errormessage
    }

    with gzip.open("favorites_data/{}_fav.json.gz".format(userid),"w") as outfile:
        outfile.write(json.dumps(writeObj).encode('utf-8'))

    with gzip.open("UserDatav2/{}_fav.json.gz".format(userid),"w") as outfile:
        outfile.write(json.dumps(writeObj).encode('utf-8'))

    return jsonify({"errorMessage" : errormessage})

@app.route('/hometimeline_prev', methods=['GET'])
def get_hometimeline_prev():
    #logger = make_logger(LOG_PATH_DEFAULT)
    worker_id = request.args.get('worker_id').strip()
    logging.info(f"Hometimeline endpoint started for : {worker_id=}")
    file_number = request.args.get('file_number').strip()
    max_id = request.args.get('max_id').strip()
    collection_started = request.args.get('collection_started').strip()
    #participant_id = request.args.get('participant_id').strip()
    #assignment_id = request.args.get('assignment_id').strip()
    #project_id = request.args.get('project_id').strip()
    num_tweets_cap = 100
    db_response = requests.get('http://127.0.0.1:5052/get_existing_user?worker_id='+str(worker_id))
    #print(db_response.json())
    db_response = db_response.json()['data']
    print("DB RESPONSE")
    print(db_response)
    access_token = db_response[0][0]
    access_token_secret = db_response[0][1]
    screenname = db_response[0][2]
    userid = db_response[0][3]
    participant_id = "NA"
    assignment_id = "NA"
    project_id = "NA"
    logging.info(f"Participant information : {worker_id=} {screenname=} {userid=} {participant_id=} {assignment_id=} {project_id=}")
    logging.info(f"Twitter information : {worker_id=} {access_token=} {access_token_secret=}")
    v2tweetobj = {}
    v1tweetobj = {}

    errormessage = "NA"

    cred = config('../configuration/config.ini','twitterapp')
    cred['token'] = access_token.strip()
    cred['token_secret'] = access_token_secret.strip()
    oauth = OAuth1Session(cred['key'],
                        client_secret=cred['key_secret'],
                        resource_owner_key=cred['token'],
                        resource_owner_secret=cred['token_secret'])
    logging.info(f"Create OAuth session : {worker_id=}")
    timeline_params_cap = {}
    for kk in timeline_params:
        timeline_params_cap[kk] = timeline_params[kk]
    timeline_params_cap["max_results"] = num_tweets_cap
    response = oauth.get("https://api.twitter.com/2/users/{}/timelines/reverse_chronological".format(userid), params = timeline_params_cap)
    logging.info(f"Got response from Twitter API : {worker_id=}")
    print("RESPONSE TEXT!!!")
    print(response.text)
    if response.text == '{"errors":[{"code":89,"message":"Invalid or expired token."}]}':
        errormessage = "Invalid Token"
        logging.info(f"Invalid Token error : {worker_id=}")

    if response.text == "{'errors': [{'message': 'Rate limit exceeded', 'code': 88}]}":
        errormessage = "Rate Limit Exceeded"
        logging.info(f"Rate limit exceeded error : {worker_id=}")

    if errormessage == "NA":

        v2tweetobj_loaded = json.loads(response.text)

        if max_id != "INITIAL":
            for section in v2tweetobj_loaded.keys():
                if section == "data":
                    v2tweetobj["data"] = []
                    for v2tweet in v2tweetobj_loaded["data"]:
                        if int(v2tweet["id"]) > int(max_id):
                            v2tweetobj["data"].append(v2tweet)
                else:
                    v2tweetobj[section] = v2tweetobj_loaded[section]
        else:
            v2tweetobj = v2tweetobj_loaded

        v1tweetobj = convertv2tov1(v2tweetobj,cred)
        logging.info(f"Converted Version 2 Twitter JSON object to Version 1 : {worker_id=}")


    now_session_start = datetime.datetime.now()
    session_start = now_session_start.strftime('%Y-%m-%dT%H:%M:%S')

    collection_started_store = collection_started
    if collection_started == "INITIAL":
        collection_started_store = session_start

    newest_id = ""
    if "meta" in v2tweetobj.keys():
        if "newest_id" in v2tweetobj["meta"]:
            newest_id = v2tweetobj["meta"]["newest_id"]

    userobj = {
        "screen_name" : screenname,
        "twitter_id" : userid
    }

    queries = compose_queries_512_chars(screenname)
    user_eng_queries = []
    for qq in queries:
        user_eng_queries.append({'query':qq,'since_id':'0','next_token':'##START##'})

    logging.info(f"Generated queries for pulling engagement data : {worker_id=}")

    writeObj = {
        "MTurkId" : participant_id,
        "MTurkHitId" : assignment_id,
        "MTurkAssignmentId" : project_id,
        "collectionStarted" : collection_started_store,
        "timestamp" : session_start,
        "source": "pilot3",
        "accessToken": access_token,
        "accessTokenSecret": access_token_secret,
        "latestTweetId": newest_id,
        "worker_id": worker_id,
        "userObject": userobj,
        "homeTweets" : v1tweetobj,
        "errorMessage" : errormessage
    }

    writeObjv2 = {
        "MTurkId" : participant_id,
        "MTurkHitId" : assignment_id,
        "MTurkAssignmentId" : project_id,
        "collectionStarted" : collection_started_store,
        "timestamp" : session_start,
        "source": "pilot3",
        "accessToken": access_token,
        "accessTokenSecret": access_token_secret,
        "latestTweetId": newest_id,
        "worker_id": worker_id,
        "userObject": userobj,
        "homeTweets" : v2tweetobj,
        "errorMessage" : errormessage
    }

    logging.info(f"Created object for writing to file : {worker_id=}")

    with gzip.open("hometimeline_data/{}_home_{}.json.gz".format(userid,file_number),"w") as outfile:
        outfile.write(json.dumps(writeObj).encode('utf-8'))

    with gzip.open("UserDatav2/{}_home_{}.json.gz".format(userid,file_number),"w") as outfile:
        outfile.write(json.dumps(writeObjv2).encode('utf-8'))

    logging.info(f"Wrote object to file : {worker_id=}")

    if "data" in v2tweetobj.keys():
        logging.info(f"Started steps for database insertion : {worker_id=}")
        feed_tweets,feed_tweets_v2 = filter_tweets(v1tweetobj,v2tweetobj["data"])
        logging.info(f"Filtered Tweets to remove duplicates : {worker_id=}")
        db_tweet_payload = []
        for (i,tweet) in enumerate(feed_tweets):
            db_tweet = {'tweet_id':tweet["id"],'tweet_json':tweet, 'tweet_json_v2':feed_tweets_v2[i]}
            db_tweet_payload.append(db_tweet)
        db_response = requests.get('http://127.0.0.1:5052/get_existing_tweets_all?worker_id='+str(worker_id))
        if db_response.json()['data'] != "NEW":
            existing_tweets = [response[6] for response in db_response.json()['data']]
            feed_tweets.extend(existing_tweets)
        logging.info(f"Got existing tweets : {worker_id=}")
        feed_tweets = feed_tweets[0:len(feed_tweets)-10]
        absent_tweets = feed_tweets[-10:]
        feed_tweets_chronological = []
        feed_tweets_chronological_score = []
        for tweet in feed_tweets:
            feed_tweets_chronological.append(tweet)
            feed_tweets_chronological_score.append(-100)
        db_tweet_chronological_payload = []
        db_tweet_chronological_attn_payload = []
        rankk = 0
        for (i,tweet) in enumerate(feed_tweets_chronological):
            if type(tweet) == float:
                continue
            page = int(rankk/10)
            rank_in_page = (rankk%10) + 1
            db_tweet = {
                'fav_before':str(tweet['favorited']),
                'tid':str(tweet["id"]),
                'rtbefore':str(tweet['retweeted']),
                'page':page,
                'rank':rank_in_page,
                'predicted_score':feed_tweets_chronological_score[i]
            }
            db_tweet_chronological_payload.append(db_tweet)
            rankk = rankk + 1
        finalJson = []
        finalJson.append(db_tweet_payload)
        finalJson.append(db_tweet_chronological_payload)
        finalJson.append(db_tweet_chronological_attn_payload)
        finalJson.append(worker_id)
        finalJson.append(screenname)
        logging.info(f"Created finalJSON for insertion : {worker_id=}")
        requests.post('http://127.0.0.1:5052/insert_timelines_attention_chronological',json=finalJson)
        logging.info(f"Completed database insertion : {worker_id=}")

    else:
        errormessage = errormessage + " No data in v2 tweet object"

    screen_2_tweet_ids = []
    db_response_tweets_screen_2 = requests.get('http://127.0.0.1:5052/get_tweets_screen_2')
    if db_response_tweets_screen_2.json()['data'] != "NEW":
        screen_2_tweet_ids = [response[0] for response in db_response_tweets_screen_2.json()['data']]
    db_screen_2_tweet_payload = []
    rankk = 0
    for tweet_id in screen_2_tweet_ids:
        page = int(rankk/10)
        rank_in_page = (rankk%10) + 1
        db_tweet = {
            'fav_before':False,
            'tid':tweet_id,
            'rtbefore':False,
            'page':page,
            'rank':rank_in_page,
            'predicted_score':-100
        }
        db_screen_2_tweet_payload.append(db_tweet)
        rankk = rankk + 1
    finalJson = []
    finalJson.append(db_screen_2_tweet_payload)
    finalJson.append(worker_id)
    finalJson.append(screenname)
    logging.info(f"Created finalJSON for insertion screen 2 : {worker_id=}")
    requests.post('http://127.0.0.1:5052/insert_timelines_screen_2_not_control',json=finalJson)
    logging.info(f"Completed database insertion : {worker_id=}")

    return jsonify({"errorMessage" : errormessage})

@app.route('/usertimeline', methods=['GET'])
def get_usertimeline():
    print("USERTIMELINE CALLED!!!")
    worker_id = request.args.get('worker_id').strip()
    db_response = requests.get('http://127.0.0.1:5052/get_existing_user?worker_id='+str(worker_id))
    #print(db_response.json())
    db_response = db_response.json()['data']
    access_token = db_response[0][0]
    access_token_secret = db_response[0][1]
    screenname = db_response[0][2]
    userid = db_response[0][3]
    participant_id = "NA"
    assignment_id = "NA"
    project_id = "NA"
    v2tweetobj = {}
    v1tweetobj = {}

    errormessage = "NA"

    cred = config('../configuration/config.ini','twitterapp')
    cred['token'] = access_token.strip()
    cred['token_secret'] = access_token_secret.strip()
    oauth = OAuth1Session(cred['key'],
                        client_secret=cred['key_secret'],
                        resource_owner_key=cred['token'],
                        resource_owner_secret=cred['token_secret'])
    response = oauth.get("https://api.twitter.com/2/users/{}/tweets".format(userid), params = timeline_params_engagement)
    if response.text == '{"errors":[{"code":89,"message":"Invalid or expired token."}]}':
        errormessage = "Invalid Token"

    if response.text == "{'errors': [{'message': 'Rate limit exceeded', 'code': 88}]}":
        errormessage = "Rate Limit Exceeded"

    if errormessage == "NA":
        v2tweetobj = json.loads(response.text)
        with gzip.open("UserDatav2/{}_user.json.gz".format(userid),"w") as outfile:
            outfile.write(json.dumps(v2tweetobj).encode('utf-8'))
        v1tweetobj = convertv2tov1(v2tweetobj,cred)

    newest_id = ""
    if "meta" in v2tweetobj.keys():
        newest_id = v2tweetobj["meta"]["newest_id"]

    userobj = {
        "screen_name" : screenname,
        "twitter_id" : userid
    }

    now_session_start = datetime.datetime.now()
    session_start = now_session_start.strftime('%Y-%m-%dT%H:%M:%S')

    writeObj = {
        "MTurkId" : participant_id,
        "MTurkHitId" : assignment_id,
        "MTurkAssignmentId" : project_id,
        "timestamp" : session_start,
        "source": "pilot3",
        "accessToken": access_token,
        "accessTokenSecret": access_token_secret,
        "latestTweetId": newest_id,
        "worker_id": worker_id,
        "userObject": userobj,
        "userTweets" : v1tweetobj,
        "errorMessage" : errormessage
    }

    with gzip.open("usertimeline_data/{}_user.json.gz".format(userid),"w") as outfile:
        outfile.write(json.dumps(writeObj).encode('utf-8'))

    #with gzip.open("UserDatav2/{}_user.json.gz".format(userid),"w") as outfile:
    #    outfile.write(json.dumps(v2tweetobj).encode('utf-8'))

    hoaxy_config = config('../configuration/config.ini','hoaxy_database')
    hoaxy_tweets,err_message = get_hoaxy_engagement(userid,hoaxy_config)

    if err_message != "NA":
        errormessage = errormessage + " HOAXY ERROR : " + err_message

    if hoaxy_tweets:
        writeObjHoaxy = {
            "MTurkId" : participant_id,
            "MTurkHitId" : assignment_id,
            "MTurkAssignmentId" : project_id,
            "timestamp" : session_start,
            "source": "pilot3",
            "accessToken": access_token,
            "accessTokenSecret": access_token_secret,
            "latestTweetId": newest_id,
            "worker_id": worker_id,
            "userObject": userobj,
            "userTweetsHoaxy" : hoaxy_tweets,
            "errorMessage" : err_message
        }

        with gzip.open("usertimeline_hoaxy_data/{}_user_hoaxy.json.gz".format(userid),"w") as outfile:
            outfile.write(json.dumps(writeObj).encode('utf-8'))

    return jsonify({"errorMessage" : errormessage})


@app.route('/favorites', methods=['GET'])
def get_favorites():
    worker_id = request.args.get('worker_id').strip()
    db_response = requests.get('http://127.0.0.1:5052/get_existing_user?worker_id='+str(worker_id))
    #print(db_response.json())
    db_response = db_response.json()['data']
    access_token = db_response[0][0]
    access_token_secret = db_response[0][1]
    screenname = db_response[0][2]
    userid = db_response[0][3]
    participant_id = "NA"
    assignment_id = "NA"
    project_id = "NA"
    v2tweetobj = {}
    v1tweetobj = {}

    errormessage = "NA"

    cred = config('../configuration/config.ini','twitterapp')
    cred['token'] = access_token.strip()
    cred['token_secret'] = access_token_secret.strip()
    oauth = OAuth1Session(cred['key'],
                        client_secret=cred['key_secret'],
                        resource_owner_key=cred['token'],
                        resource_owner_secret=cred['token_secret'])
    response = oauth.get("https://api.twitter.com/2/users/{}/liked_tweets".format(userid), params = timeline_params_engagement)
    if response.text == '{"errors":[{"code":89,"message":"Invalid or expired token."}]}':
        errormessage = "Invalid Token"

    if response.text == "{'errors': [{'message': 'Rate limit exceeded', 'code': 88}]}":
        errormessage = "Rate Limit Exceeded"

    if errormessage == "NA":
        v2tweetobj = json.loads(response.text)
        with gzip.open("UserDatav2/{}_fav.json.gz".format(userid),"w") as outfile:
            outfile.write(json.dumps(v2tweetobj).encode('utf-8'))
        v1tweetobj = convertv2tov1(v2tweetobj,cred)
        print(len(v1tweetobj))

    newest_id = ""
    #if "meta" in v2tweetobj.keys():
    #    newest_id = v2tweetobj["meta"]["newest_id"]

    userobj = {
        "screen_name" : screenname,
        "twitter_id" : userid
    }

    now_session_start = datetime.datetime.now()
    session_start = now_session_start.strftime('%Y-%m-%dT%H:%M:%S')

    writeObj = {
        "MTurkId" : participant_id,
        "MTurkHitId" : assignment_id,
        "MTurkAssignmentId" : project_id,
        "timestamp" : session_start,
        "source": "pilot3",
        "accessToken": access_token,
        "accessTokenSecret": access_token_secret,
        "latestTweetId": newest_id,
        "worker_id": worker_id,
        "userObject": userobj,
        "likedTweets" : v1tweetobj,
        "errorMessage" : errormessage
    }

    with gzip.open("favorites_data/{}_fav.json.gz".format(userid),"w") as outfile:
        outfile.write(json.dumps(writeObj).encode('utf-8'))

    #with gzip.open("UserDatav2/{}_fav.json.gz".format(userid),"w") as outfile:
    #    outfile.write(json.dumps(v2tweetobj).encode('utf-8'))

    return jsonify({"errorMessage" : errormessage})


@app.route('/getfeed', methods=['GET'])
def get_feed():
    time_now = datetime.datetime.now()
    worker_id = str(request.args.get('worker_id')).strip()
    print("WORKER ID IN GET FEED!!!")
    print(worker_id)
    experimental_condition_val = ""
    if worker_id in experimental_condition.keys():
        experimental_condition_val = experimental_condition[worker_id]
    feedtype = 'M'
    if experimental_condition_val == 'treatment':
        feedtype = 'L'
    attn = int(request.args.get('attn'))
    page = int(request.args.get('page'))
    session_id = -1
    if attn == 0 and page == 0:
        insert_session_payload = {'worker_id': worker_id}
        resp_session_id = requests.get('http://127.0.0.1:5052/insert_session',params=insert_session_payload)
        session_id = resp_session_id.json()["data"]
        session_id_store[worker_id] = session_id
        #db_response_attn = requests.get('http://127.0.0.1:5052/get_existing_attn_tweets_new?worker_id='+str(worker_id)+"&page=NA&feedtype="+feedtype)
        #db_response_attn = db_response_attn.json()['data']
        db_response_timeline = requests.get('http://127.0.0.1:5052/get_existing_tweets_new?worker_id='+str(worker_id)+"&page=NA&feedtype="+feedtype)
        db_response_timeline = db_response_timeline.json()['data']
        attn_payload = []
        attn_pages = []
        #for attn_tweet in db_response_attn:
        #    db_tweet = {
        #        'tweet_id': attn_tweet[0],
        #        'page' : attn_tweet[2],
        #        'rank' : attn_tweet[3],
        #        'present' : attn_tweet[1]
        #    }
        #    attn_payload.append(db_tweet)
        #    attn_pages.append(int(attn_tweet[2]))
        #max_page_store[worker_id] = max(attn_pages)
        max_page_store[worker_id] = 1
        timeline_payload = []
        for timeline_tweet in db_response_timeline:
            db_tweet = {
                'fav_before': timeline_tweet[2],
                'tid' : timeline_tweet[0],
                'rtbefore' : timeline_tweet[3],
                'page' : timeline_tweet[4],
                'rank' : timeline_tweet[5],
                'predicted_score' : timeline_tweet[6]
            }
            timeline_payload.append(db_tweet)
        finalJson = []
        finalJson.append(session_id)
        finalJson.append(feedtype)
        finalJson.append(timeline_payload)
        finalJson.append(attn_payload)
        requests.post('http://127.0.0.1:5052/insert_timelines_attention_in_session',json=finalJson)
        db_response_timeline_screen_2 = requests.get('http://127.0.0.1:5052/get_existing_tweets_new_screen_2?worker_id='+str(worker_id)+"&page=NA&feedtype="+feedtype)
        db_response_timeline_screen_2 = db_response_timeline_screen_2.json()['data']
        timeline_payload = []
        for timeline_tweet in db_response_timeline_screen_2:
            db_tweet = {
                'tid' : timeline_tweet[0],
                'page' : timeline_tweet[4],
                'rank' : timeline_tweet[5],
                'predicted_score' : timeline_tweet[6]
            }
            timeline_payload.append(db_tweet)
        finalJson = []
        finalJson.append(session_id)
        finalJson.append(feedtype)
        finalJson.append(timeline_payload)
        requests.post('http://127.0.0.1:5052/insert_timelines_attention_in_session_screen_2',json=finalJson)
    else:
        session_id = session_id_store[worker_id]
    if attn == 1:
        print("Here!!")
        print(worker_id)
        print(page)
        db_response = requests.get('http://127.0.0.1:5052/get_existing_tweets_new_screen_2?worker_id='+str(worker_id)+"&page="+str(page)+"&feedtype="+feedtype)
        db_response = db_response.json()['data']
        if db_response == "NEW":
            feed_json = []
            feed_json.append({"anything_present":"NO"})
            return jsonify(feed_json)
        public_tweets = [d[4] for d in db_response]
        public_tweets_v2 = [d[4] for d in db_response]
        domains = [d[6] for d in db_response]
        if len(db_response[0]) > 5:
            public_tweets_v2 = [d[5] for d in db_response]    
    else:
        print("page:::")
        print(page)
        print(worker_id)   
        db_response = requests.get('http://127.0.0.1:5052/get_existing_tweets_new?worker_id='+str(worker_id)+"&page="+str(page)+"&feedtype="+feedtype)
        db_response = db_response.json()['data']
        if db_response == "NEW":
            feed_json = []
            feed_json.append({"anything_present":"NO"})
            return jsonify(feed_json)
        public_tweets = [d[4] for d in db_response]
        public_tweets_v2 = [d[4] for d in db_response]
        domains = [d[6] for d in db_response]
        if len(db_response[0]) > 5:
            public_tweets_v2 = [d[5] for d in db_response]

    feed_json = []
    rankk = 1

    for (tweet_en,tweet) in enumerate(public_tweets): # Modify what tweet is for this loop in order to change the logic ot use our data or twitters.

        # Checking for an image in the tweet. Adds all the links of any media type to the eimage list.
        #tweet_v2 = public_tweets_v2[tweet_en]
        #if contains_video(tweet_v2):
        #    print("Skipped Video for tweet id : "+str(tweet_v2["id"]))
        #    continue
        actor_name = tweet["user"]["name"]
        full_text = tweet["full_text"]
        url_start = []
        url_end = []
        url_display = []
        url_extend = []
        url_actual = []
        domain_present = domains[tweet_en]
        if domain_present:
            domain_present = 'From ' + domain_present
        else:
            domain_present = ''
        if "entities" in tweet.keys():
            if "urls" in tweet["entities"]:
                for url_dict in tweet["entities"]["urls"]:
                    url_start.append(url_dict["indices"][0])
                    url_end.append(url_dict["indices"][1])
                    url_display.append(url_dict["display_url"])
                    url_extend.append(url_dict["expanded_url"])
                    url_actual.append(url_dict["url"])

        last_url_arr = re.findall("(?P<url>https?://[^\s]+)", full_text)
        if last_url_arr:
            last_url = last_url_arr[-1]
            if last_url not in url_actual:
                full_text = full_text.replace(last_url,'')

        full_text_json = []
        
        if url_actual:
            normal_idx = 0
            url_idx = 0
            for i in range(len(url_start)):
                url_idx_start = url_start[i]
                full_text_json.append({"text":full_text[normal_idx:url_idx_start],"url":""})
                full_text_json.append({"text":url_extend[i],"url":url_extend[i]})
                normal_idx = url_end[i]
            if normal_idx < len(full_text):
                full_text_json.append({"text":full_text[normal_idx:len(full_text)],"url":""})
        else:
            full_text_json.append({"text":full_text,"url":""})
        
        isRetweet = False 
        retweeted_by = ""
        actor_picture = tweet["user"]["profile_image_url"]
        actor_username = tweet["user"]["screen_name"]
        tempLikes = tweet["favorite_count"]
        quoted_by = ""
        quoted_by_text = ""
        quoted_by_actor_username = ""
        quoted_by_actor_picture = ""
        isQuote = False
        try: # This will handle retweet case and nested try will handle retweeted quote
            full_text = tweet["retweeted_status"]["full_text"]
            retweeted_by = actor_name # Grab it here before changing the name
            # Now I need to check if the retweeted status is a quoted status I think. 
            try:
                full_text = tweet["retweeted_status"]["quoted_status"]["full_text"]
                quoted_by = tweet["retweeted_status"]["user"]["name"]         # name of the retweet who quoted
                quoted_by_text = tweet["retweeted_status"]["full_text"]
                quoted_by_actor_username = tweet["retweeted_status"]["user"]["screen_name"]
                quoted_by_actor_picture = tweet["retweeted_status"]["user"]["profile_image_url"]
                actor_name = tweet["retweeted_status"]["quoted_status"]["user"]["name"] # original tweeter info used below.
                actor_username = tweet["retweeted_status"]["quoted_status"]["user"]["screen_name"]
                actor_picture = tweet["retweeted_status"]["quoted_status"]["user"]["profile_image_url"]
                tempLikes = tweet["retweeted_status"]["quoted_status"]["favorite_count"]
                isQuote = True
                
            except: # if its not a quote default to normal retweet settings
                actor_name = tweet["retweeted_status"]["user"]["name"] # original tweeter info used below.
                actor_username = tweet["retweeted_status"]["user"]["screen_name"]
                actor_picture = tweet["retweeted_status"]["user"]["profile_image_url"]
                tempLikes = tweet["retweeted_status"]["favorite_count"]
                isRetweet = True
            isRetweet = True
        except:
            isRetweet = False

        if not isRetweet: # case where its not a retweet but still could be a quote.
            try:
                full_text = tweet["quoted_status"]["full_text"]
                quoted_by = tweet["user"]["name"]         # name of the person who quoted
                quoted_by_text = tweet["full_text"]
                quoted_by_actor_username = tweet["user"]["screen_name"]
                quoted_by_actor_picture = tweet["user"]["profile_image_url"]
                actor_name = tweet["quoted_status"]["user"]["name"] # original tweeter info used below.
                actor_username = tweet["quoted_status"]["user"]["screen_name"]
                actor_picture = tweet["quoted_status"]["user"]["profile_image_url"]
                #tempLikes = tweet["quoted_status"]["favorite_count"]
                isQuote = True
            except:
                isQuote = False

        entities_keys = ""
        all_urls = ""
        urls_list = []
        expanded_urls_list = []
        urls = ""
        expanded_urls = ""
        image_raw = ""
        picture_heading = ""
        picture_description = ""
        mediaArr = ""
        # Decision making for the block to retrieve article cards AND embedded images

        if isQuote and isRetweet: # Check for the case of a quote within a retweet.
            if "entities" in tweet["retweeted_status"]["quoted_status"].keys(): 
                entities_keys = tweet["retweeted_status"]["quoted_status"]["entities"].keys()
                mediaArr = tweet["retweeted_status"]["quoted_status"]['entities'].get('media',[])
            if "urls" in entities_keys:
                all_urls = tweet["retweeted_status"]["quoted_status"]["entities"]["urls"]
        elif isQuote: #  quote only case
            if "entities" in tweet["quoted_status"].keys():
                entities_keys = tweet["quoted_status"]["entities"].keys()
                mediaArr = tweet["quoted_status"]['entities'].get('media',[])
            if "urls" in entities_keys:
                all_urls = tweet["quoted_status"]["entities"]["urls"]
        elif isRetweet:
            if "entities" in tweet["retweeted_status"].keys():
                entities_keys = tweet["retweeted_status"]["entities"].keys()
                mediaArr = tweet["retweeted_status"]['entities'].get('media',[])
            if "urls" in entities_keys:
                all_urls = tweet["retweeted_status"]["entities"]["urls"]
        else:
            if "entities" in tweet.keys():
                entities_keys = tweet["entities"].keys()
                mediaArr = tweet['entities'].get('media',[])
            if "urls" in entities_keys:
                all_urls = tweet["entities"]["urls"]


        # Embedded image retrieval (edited to handle retweets also now)
        hasEmbed = False
        eimage = []
        try: # Not sure why this has an issue all of a sudden.
            flag_image = False   
            if len(mediaArr) > 0:    
                for x in range(len(mediaArr)):
                    eimage.append(mediaArr[x]['media_url'])
                    flag_image = True
                    '''
                    if mediaArr[x]['type'] == 'photo':
                        hasEmbed = True
                        if "sizes" in mediaArr[x].keys():
                            if "small" in mediaArr[x]["sizes"].keys():
                                small_width = int(mediaArr[x]["sizes"]["small"]["w"])
                                small_height = int(mediaArr[x]["sizes"]["small"]["h"])
                                small_aspect_ratio = small_height/small_width
                                if small_aspect_ratio > 0.89:
                                    if "thumb" in mediaArr[x]["sizes"].keys():
                                        eimage.append(mediaArr[x]['media_url']+':thumb')
                                    else:
                                        eimage.append(mediaArr[x]['media_url']+':small')
                                else:
                                    eimage.append(mediaArr[x]['media_url']+':small')
                            else:
                                eimage.append(mediaArr[x]['media_url'])
                        else:
                            eimage.append(mediaArr[x]['media_url'])
                        flag_image = True  
                    ''' 
            if not flag_image:
                eimage.append("") 
        except Exception as error:
            print(error)
            eimage[0] = ""


        # Try to fetch card for all non-twitter URLs, stop at first URL that returns successfully
        if "urls" in entities_keys and not hasEmbed:
            found_card = False
            urls_list = [_['url'] for _ in all_urls]
            for each_url in all_urls:
                if re.match("^https://twitter.com/.*", each_url["expanded_url"]) is not None:
                    continue # skip twitter.com URLs
                card_data = CardInfo.getCardData(each_url['expanded_url'])
                if "image" in card_data.keys():
                    image_raw = card_data['image']
                    picture_heading = card_data["title"]
                    picture_description = card_data["description"]
                    urls = each_url['url']
                    expanded_urls = each_url['expanded_url']
                    found_card = True
                    break
            if not found_card:
                urls = ""
                expanded_urls = ""

        #if isRetweet:
            #print("Is a retweet.")

        for urll in urls_list:
            full_text = full_text.replace(urll,"")
        #print(full_text)
        full_text = xml.sax.saxutils.unescape(full_text)

        body = html.unescape(full_text)
        date_string_temp = tweet['created_at']
        created_date_datetime = parser.parse(date_string_temp)
        td = (datetime.datetime.now(datetime.timezone.utc) - created_date_datetime)
        hours, remainder = divmod(td.seconds, 3600) # can we scrap this and the line below ______-------________-----________---------______--------
        minutes, seconds = divmod(remainder, 60)
        time = ""
        if minutes < 10:
            time = "-00:0"+str(minutes)
        else:
            time = "-00:"+str(minutes)
        #time.append(td.seconds)
        # Fixing the like system
        finalLikes = ""
        if (tempLikes <= 999):
            finalLikes = str(tempLikes)
        elif (tempLikes >= 1000):
            counterVar = 1
            while(True):
                if (tempLikes - 1000 > 0):
                    tempLikes = tempLikes - 1000
                    counterVar = counterVar + 1
                else:
                    finalLikes = str(counterVar) + "." + str(tempLikes)[0] + "k"
                    break

        # Fixing the retweet system
        finalRetweets = ""
        tempRetweets = tweet["retweet_count"]
        if (tempRetweets <= 999):
            finalRetweets = str(tempRetweets)
        elif (tempRetweets >= 1000):
            counterVar = 1
            while(True):
                if (tempRetweets - 1000 > 0):
                    tempRetweets = tempRetweets - 1000
                    counterVar = counterVar + 1
                else:
                    finalRetweets = str(counterVar) + "." + str(tempRetweets)[0] + "k"
                    break

        profile_link = ""
        if tweet["user"]["url"]:
            profile_link = tweet["user"]["url"]
        
        feed = {
            'body':body,
            'body_json':full_text_json,
            'likes': finalLikes,
            'urls':urls,
            'expanded_urls':expanded_urls,
            'experiment_group':'var1',
            'post_id':rankk,
            'tweet_id':str(tweet["id"]),
            'worker_id':str(worker_id), 
            'rank':str(rankk),
            'picture':image_raw.replace("http:", "https:"),
            'picture_heading':picture_heading,
            'picture_description':picture_description,
            'domain_present':domain_present,
            'actor_name':actor_name,
            'actor_picture': actor_picture.replace("http:", "https:"),
            'actor_username': actor_username,
            'time':time,
            'embedded_image': eimage[0].replace("http:", "https:"),
            'retweet_count': finalRetweets,
            'profile_link': profile_link,
            'user_retweet': str(tweet['retweeted']),
            'user_fav': str(tweet['favorited']),
            'retweet_by': retweeted_by,
            'quoted_by': quoted_by,
            'quoted_by_text' : quoted_by_text,
            'quoted_by_actor_username' : quoted_by_actor_username,
            'quoted_by_actor_picture' : quoted_by_actor_picture.replace("http:", "https:")
        }
        feed_json.append(feed)
        rankk = rankk + 1
    #last_feed_value = {'new_random_identifier' : new_random_identifier}
    #feed_json.append(last_feed_value)
    last_feed_value = {'session_id' : session_id, 'max_pages' : max_page_store[worker_id], 'anything_present' : 'YES'}
    feed_json.append(last_feed_value)
    time_diff_seconds = (datetime.datetime.now()-time_now).total_seconds()
    print("Time taken : ")
    print(time_diff_seconds)
    return jsonify(feed_json)

@app.route('/completedstatuschange', methods=['GET','POST'])
def completed_status_change():
    worker_id = str(request.args.get('worker_id')).strip()
    try:
        completed_survey[worker_id] = True
    except KeyError:
        print(f"No such worker: {worker_id}")
    return "Done!"

@app.route('/completedcheck', methods=['GET','POST'])
def completed_check():
    worker_id = str(request.args.get('worker_id')).strip()
    try:
        if not completed_survey[worker_id]:
            return "NO"
        return "YES"
    except KeyError:
        print(f"No such worker: {worker_id}")
        return "NO"

@app.errorhandler(500)
def internal_server_error(e):
    return render_template('error.html', error_message='uncaught exception'), 500

@app.after_request
def add_headers(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    return response
  
if __name__ == '__main__':
    app.run(host="0.0.0.0",port=5054)
