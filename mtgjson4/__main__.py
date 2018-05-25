#!/usr/bin/env python3

import aiohttp
import ast
import asyncio
import bs4
import contextlib
import hashlib
import itertools
import json
import pathlib
import re
import time

import mtgjson4.globals

OUTPUT_DIR = pathlib.Path(__file__).resolve().parent.parent / 'outputs'
SET_CONFIG_DIR = pathlib.Path(__file__).resolve().parent / 'set_configs'


async def ensure_content_downloaded(session, url_to_download, max_retries=3, **kwargs):
    # Ensure we can read the URL and its contents
    for retry in itertools.count():
        try:
            async with session.get(url_to_download, **kwargs) as response:
                return await response.text()
        except aiohttp.ClientError:
            if retry == max_retries:
                raise
            await asyncio.sleep(2)


async def get_checklist_urls(session, set_name):
    def page_count_for_set(html_data):
        try:
            # Get the last instance of 'pagingcontrols' and get the page
            # number from the URL it contains
            soup = bs4.BeautifulSoup(html_data, 'html.parser')
            soup = soup.select('div[class^=pagingcontrols]')[-1]
            soup = soup.findAll('a')

            # If it sees '1,2,3>' will take the '3' instead of '>'
            if '&gt;' in str(soup[-1]):
                soup = soup[-2]
            else:
                soup = soup[-1]

            num_page_links = int(str(soup).split('page=')[1].split('&')[0])
        except IndexError:
            num_page_links = 0

        return num_page_links + 1

    def url_params_for_page(page_number):
        return {
            'output': 'checklist',
            'sort': 'cn+',
            'action': 'advanced',
            'special': 'true',
            'set': f'["{set_name[0]}"]',
            'page': page_number
        }

    main_url = 'http://gatherer.wizards.com/Pages/Search/Default.aspx'
    main_params = url_params_for_page(0)

    async with session.get(main_url, params=main_params) as response:
        html = await response.text()

    return [
        (main_url, url_params_for_page(page_number))
        for page_number in range(page_count_for_set(html))
    ]


async def generate_mids_by_set(session, set_urls):
    for url, params in set_urls:
        async with session.get(url, params=params) as response:
            soup = bs4.BeautifulSoup(await response.text(), 'html.parser')

            # All cards on the page
            for card_info in soup.findAll('a', {'class': 'nameLink'}):
                yield str(card_info).split('multiverseid=')[1].split('"')[0]


async def download_cards_by_mid_list(session, set_name, multiverse_ids, loop=None):
    if loop is None:
        loop = asyncio.get_event_loop()

    main_url = 'http://gatherer.wizards.com/Pages/Card/Details.aspx'
    legal_url = 'http://gatherer.wizards.com/Pages/Card/Printings.aspx'
    foreign_url = 'http://gatherer.wizards.com/Pages/Card/Languages.aspx'

    async def build_main_part(card_mid, card_info, second_card=False):
        html = await ensure_content_downloaded(session, main_url, params=get_url_params(card_mid))

        # Parse web page so we can gather all data from it
        soup = bs4.BeautifulSoup(html, 'html.parser')
        # Get Card Multiverse ID
        card_info['multiverseid'] = int(card_mid)

        # Determine if Card is Normal, Flip, or Split
        div_name = 'ctl00_ctl00_ctl00_MainContent_SubContent_SubContent_{}'
        cards_total = len(soup.select('table[class^=cardDetails]'))
        if cards_total == 1:
            card_layout = 'normal'
        elif cards_total == 2:
            card_layout = 'double'
            if second_card:
                div_name = div_name[:-3] + '_ctl03_{}'
            else:
                div_name = div_name[:-3] + '_ctl02_{}'
                additional_cards.append(loop.create_task(build_card(card_mid, second_card=True)))
        else:
            card_layout = 'unknown'

        # Get Card Name
        name_row = soup.find(id=div_name.format('nameRow'))
        name_row = name_row.findAll('div')[-1]
        card_name = name_row.get_text(strip=True)
        card_info['name'] = card_name

        # Get other side's name for the user
        if card_layout == 'double':
            if 'ctl02' in div_name:
                other_div_name = div_name.replace('02', '03')
            else:
                other_div_name = div_name.replace('03', '02')
            other_name_row = soup.find(id=other_div_name.format('nameRow'))
            other_name_row = other_name_row.findAll('div')[-1]
            card_other_name = other_name_row.get_text(strip=True)
            card_info['names'] = [card_name, card_other_name]

        # Get Card CMC
        cmc_row = soup.find(id=div_name.format('cmcRow'))
        if cmc_row is None:
            card_info['cmc'] = 0
        else:
            cmc_row = cmc_row.findAll('div')[-1]
            card_cmc = cmc_row.get_text(strip=True)
            card_info['cmc'] = int(card_cmc)

        # Get Card Colors, Cost, and Color Identity (start)
        card_color_identity = set()
        mana_row = soup.find(id=div_name.format('manaRow'))
        if mana_row:
            mana_row = mana_row.findAll('div')[-1]
            mana_row = mana_row.findAll('img')

            card_colors = set()
            card_cost = ''

            for symbol in mana_row:
                symbol_value = symbol['alt']
                symbol_mapped = mtgjson4.globals.get_symbol_short_name(symbol_value)
                card_cost += f'{{{symbol_mapped}}}'
                if symbol_mapped in mtgjson4.globals.COLORS:
                    card_color_identity.add(symbol_mapped)
                    card_colors.add(symbol_mapped)

            # Sort field in WUBRG order
            card_colors = sorted(
                list(filter(lambda c: c in card_colors, mtgjson4.globals.COLORS)),
                key=lambda word: [mtgjson4.globals.COLORS.index(c) for c in word]
            )

            if card_colors:
                card_info['colors'] = card_colors
            if card_cost:
                card_info['manaCost'] = card_cost

        # Get Card Type(s)
        card_super_types = []
        card_types = []
        type_row = soup.find(id=div_name.format('typeRow'))
        type_row = type_row.findAll('div')[-1]
        type_row = type_row.get_text(strip=True).replace('  ', ' ')

        if '—' in type_row:
            supertypes_and_types, subtypes = type_row.split('—')
        else:
            supertypes_and_types = type_row
            subtypes = ''

        for value in supertypes_and_types.split():
            if value in mtgjson4.globals.SUPERTYPES:
                card_super_types.append(value)
            elif value in mtgjson4.globals.CARD_TYPES:
                card_types.append(value)
            else:
                raise ValueError(f'Unknown supertype or card type: {value}')

        card_sub_types = subtypes.split()

        if card_super_types:
            card_info['supertypes'] = card_super_types
        if card_types:
            card_info['types'] = card_types
        if card_sub_types:
            card_info['subtypes'] = card_sub_types
        if type_row:
            card_info['type'] = type_row

        # Get Card Text and Color Identity (remaining)
        text_row = soup.find(id=div_name.format('textRow'))
        if text_row is None:
            card_info['text'] = ''
        else:
            text_row = text_row.select('div[class^=cardtextbox]')

            card_text = ''
            for div in text_row:
                # Start by replacing all images with alternative text
                images = div.findAll('img')
                for symbol in images:
                    symbol_value = symbol['alt']
                    symbol_mapped = mtgjson4.globals.get_symbol_short_name(symbol_value)
                    symbol.replace_with(f'{{{symbol_mapped}}}')
                    if symbol_mapped in mtgjson4.globals.COLORS:
                        card_color_identity.add(symbol_mapped)

                # Next, just add the card text, line by line
                card_text += div.get_text() + '\n'

            card_info['text'] = card_text[:-1]  # Remove last '\n'

        # Sort field in WUBRG order
        card_color_identity = sorted(
            list(filter(lambda c: c in card_color_identity, mtgjson4.globals.COLORS)),
            key=lambda word: [mtgjson4.globals.COLORS.index(c) for c in word]
        )

        if card_color_identity:
            card_info['colorIdentity'] = card_color_identity

        # Get Card Flavor Text
        flavor_row = soup.find(id=div_name.format('flavorRow'))
        if flavor_row is not None:
            flavor_row = flavor_row.select('div[class^=flavortextbox]')

            card_flavor_text = ''
            for div in flavor_row:
                card_flavor_text += div.get_text() + '\n'

            card_info['flavor'] = card_flavor_text[:-1]  # Remove last '\n'

        # Get Card P/T OR Loyalty OR Hand/Life
        pt_row = soup.find(id=div_name.format('ptRow'))
        if pt_row is not None:
            pt_row = pt_row.findAll('div')[-1]
            pt_row = pt_row.get_text(strip=True)

            # If Vanguard
            if 'Hand Modifier' in pt_row:
                pt_row = pt_row.split('\xa0,\xa0')
                card_hand_mod = pt_row[0].split(' ')[-1]
                card_life_mod = pt_row[1].split(' ')[-1][:-1]

                card_info['hand'] = card_hand_mod
                card_info['life'] = card_life_mod
            elif '/' in pt_row:
                card_power, card_toughness = pt_row.split('/')
                card_info['power'] = card_power.strip()
                card_info['toughness'] = card_toughness.strip()
            else:
                card_info['loyalty'] = pt_row.strip()

        # Get Card Rarity
        rarity_row = soup.find(id=div_name.format('rarityRow'))
        rarity_row = rarity_row.findAll('div')[-1]
        card_rarity = rarity_row.find('span').get_text(strip=True)
        card_info['rarity'] = card_rarity

        # Get Card Set Number
        number_row = soup.find(id=div_name.format('numberRow'))
        if number_row is not None:
            number_row = number_row.findAll('div')[-1]
            card_number = number_row.get_text(strip=True)
            card_info['number'] = card_number

        # Get Card Artist
        artist_row = soup.find(id=div_name.format('artistRow'))
        artist_row = artist_row.findAll('div')[-1]
        card_artist = artist_row.find('a').get_text(strip=True)
        card_artists = card_artist.split('&')

        card_info['artist'] = card_artist
        if len(card_artists) > 1:
            card_info['artists'] = card_artists

        # Get Card Watermark
        watermark_row = soup.find(id=div_name.format('markRow'))
        if watermark_row is not None:
            watermark_row = watermark_row.findAll('div')[-1]
            card_watermark = watermark_row.get_text(strip=True)
            card_info['watermark'] = card_watermark

        # Get Card Reserve List Status
        if card_info['name'] in mtgjson4.globals.RESERVE_LIST:
            card_info['reserved'] = True

        # Get Card Rulings
        rulings_row = soup.find(id=div_name.format('rulingsRow'))
        if rulings_row is not None:
            rulings_dates = rulings_row.findAll('td', id=re.compile(r'\w*_rulingDate\b'))
            rulings_text = rulings_row.findAll('td', id=re.compile(r'\w*_rulingText\b'))
            card_info['rulings'] = [
                {
                    'date': ruling_date.get_text(),
                    'text': ruling_text.get_text()
                }
                for ruling_date, ruling_text in zip(rulings_dates, rulings_text)
            ]

        # Get Card Sets
        card_printings = [set_name[1]]
        sets_row = soup.find(id=div_name.format('otherSetsRow'))
        if sets_row is not None:
            images = sets_row.findAll('img')

            for symbol in images:
                this_set_name = symbol['alt'].split('(')[0].strip()

                card_printings += (
                    set_code[1] for set_code in mtgjson4.globals.GATHERER_SETS if this_set_name == set_code[0]
                )

        card_info['printings'] = card_printings

        # Get Card Variations
        variations_row = soup.find(id=div_name.format('variationLinks'))
        if variations_row is not None:
            card_variations = []

            for variations_info in variations_row.findAll('a', {'class': 'variationLink'}):
                card_variations.append(int(variations_info['href'].split('multiverseid=')[1]))

            with contextlib.suppress(ValueError):
                card_variations.remove(card_info['multiverseid'])  # Don't need this card's MID in its variations

            card_info['variations'] = card_variations

    async def build_legalities_part(card_mid, card_info):
        try:
            html = await ensure_content_downloaded(session, legal_url, params=get_url_params(card_mid))
        except aiohttp.ClientError as error:
            # If Gatherer errors, omit the data for now
            # This can be appended on a case-by-case basis
            if error.code == 500:
                return  # Page doesn't work, nothing we can do
            else:
                return

        # Parse web page so we can gather all data from it
        soup = bs4.BeautifulSoup(html, 'html.parser')

        # Get Card Legalities
        format_rows = soup.select('table[class^=cardList]')[1]
        format_rows = format_rows.select('tr[class^=cardItem]')
        card_formats = []
        with contextlib.suppress(IndexError):  # if no legalities, only one tr with only one td
            for div in format_rows:
                table_rows = div.findAll('td')
                card_format_name = table_rows[0].get_text(strip=True)
                card_format_legal = table_rows[1].get_text(strip=True)  # raises IndexError if no legalities

                card_formats.append({
                    'format': card_format_name,
                    'legality': card_format_legal
                })

            card_info['legalities'] = card_formats

    async def build_foreign_part(card_mid, card_info):
        try:
            html = await ensure_content_downloaded(session, foreign_url, params=get_url_params(card_mid))
        except aiohttp.ClientError as error:
            # If Gatherer errors, omit the data for now
            # This can be appended on a case-by-case basis
            if error.code == 500:
                return  # Page doesn't work, nothing we can do
            else:
                return

        # Parse web page so we can gather all data from it
        soup = bs4.BeautifulSoup(html, 'html.parser')

        # Get Card Foreign Information
        language_rows = soup.select('table[class^=cardList]')[0]
        language_rows = language_rows.select('tr[class^=cardItem]')

        card_languages = []
        for div in language_rows:
            table_rows = div.findAll('td')

            a_tag = table_rows[0].find('a')
            foreign_mid = a_tag['href'].split('=')[-1]
            card_language_mid = foreign_mid
            card_foreign_name_in_language = a_tag.get_text(strip=True)

            card_language_name = table_rows[1].get_text(strip=True)

            card_languages.append({
                'language': card_language_name,
                'name': card_foreign_name_in_language,
                'multiverseid': card_language_mid
            })

        card_info['foreignNames'] = card_languages

    async def build_id_part(card_mid, card_info):
        card_id = hashlib.sha3_256()
        card_id.update(set_name[0].encode('utf-8'))
        card_id.update(str(card_mid).encode('utf-8'))
        card_id.update(card_info['name'].encode('utf-8'))

        card_info['id'] = card_id.hexdigest()

    async def build_card(card_mid, second_card=False):
        card_info = {}

        await build_main_part(card_mid, card_info, second_card=second_card)
        await build_legalities_part(card_mid, card_info)
        await build_foreign_part(card_mid, card_info)
        await build_id_part(card_mid, card_info)

        print('Adding {0} to {1}'.format(card_info['name'], set_name[0]))
        return card_info

    def add_layouts(cards):
        for card_info in cards:
            if 'names' in card_info:
                sides = len(card_info['names'])
            else:
                sides = 1

            if sides == 1:
                if 'hand' in card_info:
                    card_layout = 'Vanguard'
                elif 'Scheme' in card_info['types']:
                    card_layout = 'Scheme'
                elif 'Plane' in card_info['types']:
                    card_layout = 'Plane'
                elif 'Phenomenon' in card_info['types']:
                    card_layout = 'Phenomenon'
                else:
                    card_layout = 'Normal'
            elif sides == 2:
                if 'transform' in card_info['text']:
                    card_layout = 'Double-Faced'
                elif 'aftermath' in card_info['text']:
                    card_layout = 'Aftermath'
                elif 'flip' in card_info['text']:
                    card_layout = 'Flip'
                elif 'split' in card_info['text']:
                    card_layout = 'Split'
                elif 'meld' in card_info['text']:
                    card_layout = 'Meld'
                else:
                    card_2_name = next(card2 for card2 in card_info['names'] if card_info['name'] != card2)
                    card_2_info = next(card2 for card2 in cards if card2['name'] == card_2_name)

                    if 'flip' in card_2_info['text']:
                        card_layout = 'Flip'
                    elif 'transform' in card_2_info['text']:
                        card_layout = 'Double-Faced'
                    else:
                        card_layout = 'Unknown'
            else:
                card_layout = 'Meld'

            card_info['layout'] = card_layout

    def get_url_params(card_mid):
        return {
            'multiverseid': card_mid,
            'printed': 'false',
            'page': 0
        }

    # start asyncio tasks for building each card
    futures = [
        loop.create_task(build_card(card_mid))
        for card_mid in multiverse_ids
    ]

    additional_cards = []

    # then wait until all of them are completed
    await asyncio.wait(futures)
    cards_in_set = []
    for future in futures:
        card_future = future.result()
        cards_in_set.append(card_future)

    with contextlib.suppress(ValueError):  # if no double-sided cards, gracefully skip
        await asyncio.wait(additional_cards)
        for future in additional_cards:
            card_future = future.result()
            cards_in_set.append(card_future)

    add_layouts(cards_in_set)

    return cards_in_set


async def apply_set_config_options(session, set_name, cards_dictionary):
    return_product = dict()

    with (SET_CONFIG_DIR / '{}.json'.format(set_name[1])).open('r') as fp:
        file_response = ast.literal_eval(fp.read())

        for key, value in file_response['SET'].items():
            return_product[key] = value

        for match_replace_rule in file_response['SET_CORRECTIONS']:
            for key, value in match_replace_rule.items():
                # TODO: Change format of set_configs to make it easier to parse
                print(key, value)

    return_product['cards'] = cards_dictionary

    return return_product


# TODO: Missing fields
# border - Only done if they don't match set (need set config)
# timeshifted - Only for timeshifted sets (need set config)
# starter - in starter deck (need set config)
async def build_set(session, set_name):
    print('BuildSet: Building Set {}'.format(set_name[0]))

    urls_for_set = await get_checklist_urls(session, set_name)
    print('BuildSet: Acquired URLs for {}'.format(set_name[0]))

    mids_for_set = [mid async for mid in generate_mids_by_set(session, urls_for_set)]
    # mids_for_set = [417835, 417836, 417837]  # DEBUG # 439335, 442051, 435172, 182290, 435173, 443154, 442767,
    print('BuildSet: Determined MIDs for {0}: {1}'.format(set_name[0], mids_for_set))

    cards_holder = await download_cards_by_mid_list(session, set_name, mids_for_set)
    print('BuildSet: Generated JSON for {}'.format(set_name[0]))

    json_ready = await apply_set_config_options(session, set_name, cards_holder)
    print('BuildSet: Applied Set Config options for {}'.format(set_name[0]))

    with (OUTPUT_DIR / '{}.json'.format(set_name[1])).open('w') as fp:
        json.dump(json_ready, fp, indent=4, sort_keys=True)
    print('BuildSet: JSON written for {0} ({1})'.format(set_name[0], set_name[1]))


async def main(loop, session):
    OUTPUT_DIR.mkdir(exist_ok=True)  # make sure outputs dir exists

    async with session:
        # start asyncio tasks for building each set
        futures = [
            loop.create_task(build_set(session, set_name))
            for set_name in mtgjson4.globals.GATHERER_SETS
        ]
        # then wait until all of them are completed
        await asyncio.wait(futures)


if __name__ == '__main__':
    start_time = time.time()

    card_loop = asyncio.get_event_loop()
    card_session = aiohttp.ClientSession(loop=card_loop, raise_for_status=True)
    card_loop.run_until_complete(main(card_loop, card_session))

    end_time = time.time()
    print('Time: {}'.format(end_time - start_time))
