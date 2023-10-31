import numpy as np
import cv2
from logger import Logger
from item.data.rarity import ItemRarity
from item.data.item_type import ItemType
from item.data.affix import Affix
from item.data.aspect import Aspect
from item.models import Item
from template_finder import search
from utils.ocr.read import image_to_text
from utils.image_operations import crop
from utils.window import screenshot
import re
import json
from rapidfuzz import process

affix_dict = dict()
with open("assets/affixes.json", "r") as f:
    affix_dict = json.load(f)

aspect_dict = dict()
with open("assets/aspects.json", "r") as f:
    aspect_dict = json.load(f)


def _closest_match(target, candidates, min_score=90):
    keys, values = zip(*candidates.items())
    result = process.extractOne(target, values)
    if result and result[1] >= min_score:
        matched_key = keys[values.index(result[0])]
        return matched_key
    return None


def _find_text_lines(img: np.ndarray):
    if img is None or img.shape[0] == 0 or img.shape[1] == 0:
        return []
    res = []
    pimg = cv2.Canny(img, 30, 150)
    pimg = cv2.dilate(pimg, np.ones((1, 9)), iterations=1)
    pimg = cv2.erode(pimg, np.ones((1, 9)), iterations=1)
    cont2, _ = cv2.findContours(pimg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for c in cont2:
        x2, y2, w2, h2 = cv2.boundingRect(c)
        if 16 < w2 < 350 and 9 < h2 < 25:
            roi = [
                max(0, int(x2) - 7),
                max(0, int(y2) - 6),
                int(w2) + 14,
                int(h2) + 12,
            ]
            res.append(roi)
    res = sorted(res, key=lambda roi: roi[1])
    return res


def _find_number(s):
    matches = re.findall(r"[+-]?(\d+\.\d+|\.\d+|\d+\.?|\d+)\%?", s)
    if "Up to a 5%" in s:
        number = matches[1] if len(matches) > 1 else None
    number = matches[0] if matches else None
    if number is not None:
        return float(number.replace("+", "").replace("%", ""))
    return None


def _clean_str(s):
    cleaned_str = re.sub(r"(\+)?\d+(\.\d+)?%?", "", s)  # Remove numbers and trailing % or preceding +
    cleaned_str = re.sub(r"[\[\]+\-:%\']", "", cleaned_str)  # Remove [ and ] and leftover +, -, %, :, '
    cleaned_str = re.sub(
        r"\((rogue|barbarian|druid|sorcerer|necromancer) only\)", "", cleaned_str
    )  # this is not included in our affix table
    cleaned_str = " ".join(cleaned_str.split())  # Remove extra spaces
    return cleaned_str


def read_descr(rarity: ItemRarity, img_item_descr: np.ndarray) -> Item:
    item = Item(rarity)

    # Detect textures (1)
    # =====================================
    if not (seperator_long := search("item_seperator_long", img_item_descr, threshold=0.87, use_grayscale=True, mode="all")).success:
        Logger.warning("Could not detect item_seperator_long.")
        screenshot("failed_seperator_long", img=img_item_descr)
        return None
    seperator_long.matches = sorted(seperator_long.matches, key=lambda match: match.center[1])
    # Mask img where seperator_long was found
    masked_search_img = img_item_descr.copy()
    for match in seperator_long.matches:
        x, y, w, h = match.region
        cv2.rectangle(masked_search_img, (x, y), (x + w, y + h), (0, 0, 0), -1)
    if not (seperator_short := search("item_seperator_short", masked_search_img, threshold=0.67, use_grayscale=True, mode="best")).success:
        Logger.warning("Could not detect item_seperator_short.")
        screenshot("failed_seperator_short", img=masked_search_img)
        return None

    # Item Type and Item Power
    # =====================================
    _, w, _ = img_item_descr.shape
    roi_top = [15, 15, w - 30, seperator_short.matches[0].center[1] - 20]
    crop_top = crop(img_item_descr, roi_top)
    text_top = _find_text_lines(crop_top)
    all_text = []
    for t in text_top:
        cropped_text = crop(crop_top, t)
        r = image_to_text(cropped_text)
        all_text.append(r.text)
    concatenated_str = " ".join(all_text).lower()
    if "item power" in concatenated_str:
        idx = concatenated_str.index("item power")
        preceding_word = concatenated_str[:idx].split()[-1]
        if preceding_word.isdigit():
            item.power = int(preceding_word)
    max_length = 0
    for item_type in ItemType:
        if item_type.value in concatenated_str:
            if len(item_type.value) > max_length:
                item.type = item_type
                max_length = len(item_type.value)
    # common mistake is that "Armor" is on a seperate line and can not be detected
    if item.type is None:
        if "chest" in concatenated_str:
            item.type = ItemType.Armor

    if item.power is None or item.type is None:
        Logger().warning(f"Could not detect ItemPower and ItemType: {concatenated_str}")
        screenshot("failed_itempower_itemtype", img=img_item_descr)
        return None

    # Detect textures (2)
    # =====================================
    if item.type in [ItemType.Helm, ItemType.Armor, ItemType.Gloves]:
        roi_bullets = [0, seperator_short.matches[0].center[1], 100, 1080]
    else:
        roi_bullets = [0, seperator_long.matches[0].center[1], 100, 1080]
    if not (
        affix_bullets := search("affix_bullet_point", img_item_descr, threshold=0.87, roi=roi_bullets, use_grayscale=True, mode="all")
    ).success:
        Logger.warning("Could not detect affix_bullet_points.")
        screenshot("failed_affix_bullet_points", img=img_item_descr)
        return None
    affix_bullets.matches = sorted(affix_bullets.matches, key=lambda match: match.center[1])
    empty_sockets = search("empty_socket", img_item_descr, threshold=0.87, roi=roi_bullets, use_grayscale=True, mode="all")
    empty_sockets.matches = sorted(empty_sockets.matches, key=lambda match: match.center[1])
    aspect_bullets = search("aspect_bullet_point", img_item_descr, threshold=0.87, roi=roi_bullets, use_grayscale=True, mode="first")
    if rarity == ItemRarity.Legendary and not aspect_bullets.success:
        Logger.warning("Could not detect aspect_bullet for a legendary item.")
        screenshot("failed_aspect_bullet", img=img_item_descr)
        return None

    # Affixes
    # =====================================
    affix_spaces = affix_bullets.matches.copy()
    if rarity == ItemRarity.Legendary:
        affix_spaces.append(aspect_bullets.matches[0])
    elif len(empty_sockets.matches) > 0:
        affix_spaces.append(empty_sockets.matches[0])
    else:
        affix_spaces.append(seperator_long.matches[-1])
    for i in range(1, len(affix_spaces)):
        next = affix_spaces[i].center
        curr = affix_spaces[i - 1].center
        dy = next[1] - curr[1] + 5
        roi_full_affix = [curr[0] + 7, max(0, curr[1] - 16), w - 30 - curr[0], dy]
        img_full_affix = crop(img_item_descr, roi_full_affix)
        text_affix = _find_text_lines(img_full_affix)
        text_affix = sorted(text_affix, key=lambda roi: roi[1])  # sort by y coordinate
        text_affix = [roi for roi in text_affix if roi[0] <= 20]  # filters out stuff like required level text on the right
        all_text = []
        for t in text_affix:
            cropped_text = crop(img_full_affix, t)
            r = image_to_text(cropped_text)
            all_text.append(r.text)
        concatenated_str = " ".join(all_text).lower()
        cleaned_str = _clean_str(concatenated_str)

        found_key = _closest_match(cleaned_str, affix_dict)
        found_value = _find_number(concatenated_str)

        if found_key is not None:
            item.affixes.append(Affix(found_key, concatenated_str, found_value))
        else:
            Logger.warning(f"Could not find affix: {cleaned_str}")
            screenshot("failed_affixes", img=img_item_descr)
            return None

    # Aspect
    # =====================================
    if rarity == ItemRarity.Legendary:
        ab = aspect_bullets.matches[0].center
        bottom_limit = empty_sockets.matches[0].center[1] if len(empty_sockets.matches) > 0 else seperator_long.matches[-1].center[1]
        # in case of scroll down is visible the bottom seperator is not visible
        if bottom_limit < ab[1]:
            bottom_limit = img_item_descr.shape[0]
        dy = bottom_limit - ab[1]
        roi_full_aspect = [ab[0] + 7, max(0, ab[1] - 16), w - 30 - ab[0], dy]
        img_full_aspect = crop(img_item_descr, roi_full_aspect)
        text_aspect = _find_text_lines(img_full_aspect)
        text_aspect = sorted(text_aspect, key=lambda roi: roi[1])  # sort by y coordinate
        text_aspect = [roi for roi in text_aspect if roi[0] <= 20]  # filters out stuff like required level text on the right
        all_text = []
        for t in text_aspect:
            cropped_text = crop(img_full_aspect, t)
            r = image_to_text(cropped_text)
            all_text.append(r.text)
        concatenated_str = " ".join(all_text).lower()
        cleaned_str = _clean_str(concatenated_str)

        found_key = _closest_match(cleaned_str, aspect_dict, min_score=77)
        found_value = _find_number(concatenated_str)

        if found_key is not None:
            item.aspect = Aspect(found_key, concatenated_str, found_value)
        else:
            Logger.warning(f"Could not find aspect: {cleaned_str}")
            screenshot("failed_aspect", img=img_item_descr)
            return None

    return item
