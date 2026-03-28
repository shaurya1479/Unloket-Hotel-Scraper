# Unloket-Hotel-Scraper
This is a scraper I built to find and qualify hotel leads for Unloket. The idea was to automate the early part of outreach so instead of manually digging through Google Maps and hotel websites, I could pull everything into one place and quickly see which hotels are actually worth reaching out to.

At a high level, the script starts with Google Maps. It searches for hotels in a given city, scrolls through the results, and clicks into each listing one by one. From each hotel page, it pulls basic info like the name, address, rating, number of reviews, phone number, and website if available. It also checks the About and Reviews sections to get a sense of amenities and what guests are complaining about.

If a hotel has a website, the script then visits it and crawls a handful of pages. It looks for things like emails, phone numbers, room count, staff names, and any signs of chatbot tools or guest messaging systems. It also tries to pick up details like amenities, meeting spaces, or whether the hotel is more business or tourist focused. The crawling logic is pretty simple and not perfect, but it covers most common site structures.

After collecting all of that, the script runs a scoring function. This is where it tries to figure out which hotels are good leads. It looks at things like size, price range, review volume, whether they already use a chatbot, and whether guests mention pain points like slow service or poor communication. Based on that, each hotel gets a score and is labeled as hot, warm, or cold.

The final output is a CSV file and a simple HTML dashboard. The CSV is useful if you want to filter or import into something else. The dashboard is more visual and lets you quickly scan through hotels, sort by score, and click into details.

To run it, you just need Python and Playwright installed. Then you can do something like

python3 scraper.py --city "Chicago" --max 30

That will scrape up to 30 hotels in the city you pass in and generate the outputs.

A couple things to keep in mind. This relies heavily on Google Maps and website structures, so parts of it can break if those change. Some fields like room count or emails are best effort and will not always be accurate. It is meant to save time and get you most of the way there but might still require manual verification occasiaonally.
