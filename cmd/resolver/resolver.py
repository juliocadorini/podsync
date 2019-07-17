import os
import youtube_dl
import boto3
from datetime import datetime, time
from dateutil.relativedelta import relativedelta

ANONYMOUS_FEED_REQUESTS_LIMIT = 100


class InvalidUsage(Exception):
    pass


class QuotaExceeded(Exception):
    pass


dynamodb = boto3.resource('dynamodb')

feeds_table = dynamodb.Table(os.getenv('DYNAMO_FEEDS_TABLE_NAME', 'Feeds'))
counter_table = dynamodb.Table(os.getenv('DYNAMO_RESOLVE_COUNTERS_TABLE', 'ResolveCounters'))

opts = {
    'quiet': True,
    'no_warnings': True,
    'forceurl': True,
    'simulate': True,
    'skip_download': True,
    'call_home': False,
    'nocheckcertificate': True
}

url_formats = {
    'youtube': 'https://youtube.com/watch?v={}',
    'vimeo': 'https://vimeo.com/{}',
}


def download(feed_id, video_id):
    if not feed_id:
        raise InvalidUsage('Invalid feed id')

    # Remove extension and check if video id is ok
    video_id = os.path.splitext(video_id)[0]
    if not video_id:
        raise InvalidUsage('Invalid video id')

    # Query feed metadata info from DynamoDB
    item = _get_metadata(feed_id)

    # Update resolve requests counter
    count = _update_resolve_counter(feed_id)
    level = int(item['featurelevel'])
    if count > ANONYMOUS_FEED_REQUESTS_LIMIT and level == 0:
        raise QuotaExceeded('Too many requests. Daily limit is %d. Consider upgrading account to get unlimited '
                            'access' % ANONYMOUS_FEED_REQUESTS_LIMIT)

    # Build URL
    provider = item['provider']
    tpl = url_formats[provider]
    if not tpl:
        raise InvalidUsage('Invalid feed')
    url = tpl.format(video_id)

    redirect_url = _resolve(url, item)
    return redirect_url


def _get_metadata(feed_id):
    response = feeds_table.get_item(
        Key={'HashID': feed_id},
        ProjectionExpression='#P,#F,#Q,#L',
        ExpressionAttributeNames={
            '#P': 'Provider',
            '#F': 'Format',
            '#Q': 'Quality',
            '#L': 'FeatureLevel',
        },
    )

    item = response['Item']

    # Make dict keys lowercase
    return dict((k.lower(), v) for k, v in item.items())


def _update_resolve_counter(feed_id):
    if not feed_id:
        return

    now = datetime.utcnow()
    day = now.strftime('%Y%m%d')

    expires = now + relativedelta(months=3)

    response = counter_table.update_item(
        Key={
            'FeedID': feed_id,
            'Day': int(day),
        },
        UpdateExpression='ADD #count :one SET #exp = if_not_exists(#exp, :ttl)',
        ExpressionAttributeNames={
            '#count': 'Count',
            '#exp': 'Expires',
        },
        ExpressionAttributeValues={
            ':one': 1,
            ':ttl': int(expires.timestamp()),
        },
        ReturnValues='UPDATED_NEW',
    )

    attrs = response['Attributes']
    return attrs['Count']


def _resolve(url, metadata):
    if not url:
        raise InvalidUsage('Invalid URL')

    print('Resolving %s' % url)

    try:
        provider = metadata['provider']

        with youtube_dl.YoutubeDL(opts) as ytdl:
            info = ytdl.extract_info(url, download=False)
            if provider == 'youtube':
                return _yt_choose_url(ytdl, info, metadata)
            elif provider == 'vimeo':
                return _vimeo_choose_url(info, metadata)
            else:
                raise ValueError('undefined provider')
    except Exception as e:
        print(e)
        raise


def _yt_choose_url(ytdl, info, metadata):
    is_video = metadata['format'] == 'video'
    is_high_quality = metadata['quality'] == 'high'

    if is_video:
        fmt = 'best[ext=mp4]' if is_high_quality else 'worst[ext=mp4]'
    else:
        fmt = 'bestaudio' if is_high_quality else 'worstaudio'

    selector = ytdl.build_format_selector(fmt)
    selected = next(selector(info))
    if 'fragment_base_url' in selected:
        return selected['fragment_base_url']

    return selected['url']


def _vimeo_choose_url(info, metadata):
    # Query formats with 'extension' = mp4 and 'format_id' = http-1080p/http-720p/../http-360p
    fmt_list = [x for x in info['formats'] if x['ext'] == 'mp4' and x['format_id'].startswith('http-')]

    ordered = sorted(fmt_list, key=lambda x: x['width'], reverse=True)
    is_high_quality = metadata['quality'] == 'high'
    item = ordered[0] if is_high_quality else ordered[-1]

    return item['url']
