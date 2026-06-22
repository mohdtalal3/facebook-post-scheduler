import requests
from urllib.parse import urlparse
from html import unescape


def get_wp_post_data(post_url):
    parsed = urlparse(post_url)

    # Domain
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    # Slug from URL
    slug = parsed.path.strip("/").split("/")[-1]

    # WordPress REST API endpoint
    api_url = f"{base_url}/wp-json/wp/v2/posts?slug={slug}&_embed"

    response = requests.get(
        api_url,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30
    )
    response.raise_for_status()

    posts = response.json()

    if not posts:
        raise Exception("Post not found")

    post = posts[0]

    # Decode HTML entities
    title = unescape(post["title"]["rendered"])
    content_html = unescape(post["content"]["rendered"])
    link = post["link"]

    # Featured image
    image = None
    embedded = post.get("_embedded", {})

    if "wp:featuredmedia" in embedded:
        image = embedded["wp:featuredmedia"][0].get("source_url")

    return {
        "title": title,
        "image": image,
        "link": link,
        "html": content_html
    }


# # Example
# url = "https://www.aisleofshame.com/new-and-returning-items-of-the-week-at-trader-joes-2/"

# data = get_wp_post_data(url)

# print("TITLE:")
# print(data["title"])

# print("\nIMAGE:")
# print(data["image"])

# print("\nLINK:")
# print(data["link"])

