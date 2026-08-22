# Bundled data

## us_zip_centroids.csv.gz

ZIP (ZCTA) centroids for the United States: `zip,latitude,longitude`, sorted by
ZIP, gzipped.

Source: [US Census Bureau 2023 ZCTA Gazetteer](https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2023_Gazetteer/2023_Gaz_zcta_national.zip).
A work of the US federal government, and so in the public domain.

Bundled rather than looked up online because Bask commonly runs on a LAN with no
route out, and because a keeper's home coordinates are not something to send to a
third party in order to find out when the sun sets.

Coordinates are kept to three decimal places, which is about 100 m — far finer
than sunrise timing needs.

Aggregating to three-digit ZIP prefixes would shrink this to 17 KB and is
accurate to under four minutes for 95% of ZIPs, but it is wrong by up to two and
a half hours in Alaska and Hawaii, whose prefixes span thirty degrees of
longitude. The full table costs 252 KB and is right everywhere.
