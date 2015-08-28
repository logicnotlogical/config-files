#!/usr/bin/env python2

"""
Theme generator/manager.
 
Can generate themes from:
 
- Xresources-style color files
- Sweyla's site, e.g. for http://sweyla.com/themes/seed/693812/ -> 693812
- Images, in which case it will use k-means to get colors
 
Requires:
 
- ~/.config/themer/templates/ directory w/one valid set of templates
 
Assumes:
 
- you're using "any color you like" icon set
- probably a lot of other things
"""

from collections import namedtuple
import colorsys
import itertools
import logging
import math
import optparse
import os
import random
import re
import shutil
import sys
import urllib2
import yaml

from jinja2 import Environment, FileSystemLoader
try:
    import Image, ImageDraw
except ImportError:
    from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)


DEFAULT_CONTEXT_CONFIG = {
    'primary': 'magenta',
    'secondary': 'green',
    'tertiary': 'blue',
}
CONFIG_DIR = os.getenv('XDG_CONFIG_HOME', os.path.join(os.getenv('HOME'), '.config'))
THEMER_ROOT = os.path.join(CONFIG_DIR, 'themer')
TEMPLATE_ROOT = os.path.join(THEMER_ROOT, 'templates')

def dict_update(parent, child):
    """Recursively update parent dict with child dict."""
    for key, value in child.iteritems():
        if key in parent and isinstance(parent[key], dict):
            parent[key] = dict_update(parent[key], value)
        else:
            parent[key] = value
    return parent

def read_config(config_file):
    """Read a YAML config file."""
    logger.debug('Reading config file: %s' % config_file)
    config_dir = os.path.dirname(config_file)
    base_config = {}
    with open(config_file) as fh:
        data = yaml.load(fh)

    if data.get('extends'):
        parent_config = os.path.join(config_dir, data['extends'])
        base_config = read_config(parent_config)

    return dict_update(base_config, data)

def render_templates(template_dir, files, context):
    """Render templates from `template_dir`."""
    env = Environment(loader=FileSystemLoader(template_dir))
    logger.debug('Jinja environment configured for: %s' % template_dir)

    for src, dest in files.items():
        dir_name = os.path.dirname(dest)
        if not os.path.exists(dir_name):
            logger.debug('Creating directory %s' % dir_name)
            os.makedirs(dir_name)
        if src.endswith(('tpl', 'conf')):
            logger.info('Writing %s -> %s' % (src, dest))
            template = env.get_template(src)
            with open(dest, 'w') as fh:
                fh.write(template.render(**context).encode('utf-8'))
        else:
            logger.info('Copying %s -> %s' % (src, dest))
            shutil.copy(os.path.join(template_dir, src), dest)

def munge_context(variables, colors):
    context = {}
    context.update(variables)

    # Handle case when a variable may reference a color, e.g.
    # `primary` = `alt_red`, then `primary` = `#fd1a2b`
    for key, value in context.items():
        if value in colors:
            context[key] = colors[value]
    context.update(colors)

    for key, value in DEFAULT_CONTEXT_CONFIG.items():
        if key not in context:
            context[key] = context[value]

    return context

def wallfix(directory, colors):
    """Look in `directory` for file named `wallpaper.xxx` and set it."""
    wallpaper = None
    for filename in os.listdir(directory):
        if filename.startswith('wallpaper'):
            wallpaper = filename
            break

    if not wallpaper:
        logger.info('No wallpaper found, generating new one.')
        wallpaper = create_wallpaper(colors, directory)

    logger.info('Setting %s as wallpaper' % wallpaper)
    path = os.path.join(directory, wallpaper)
    os.system('wallfix %s' % path)

def hex_to_rgb(h):
    h = h.lstrip('#')
    return tuple(map(lambda n: int(n, 16), [h[i:i+2] for i in range(0, 6, 2)]))

def rgb_to_hex(rgb):
    return '#%s' % ''.join(('%02x' % p for p in rgb))

def create_wallpaper(colors, template_dir, w=1920, h=1200, filename='wallpaper.png'):
    rectangles = (
        # x1, y1, x2, y2 -- in percents
        ('red', [0, 30.0, 3.125, 72.5]), # LEFT
        ('green', [50, 0, 76.5625, 12.5]), # TOP
        ('yellow', [96.875, 30.0, 100, 72.5]), # RIGHT
        ('magenta', [23.4375, 25.0, 50, 30.0]), # MID TOP LEFT
        ('white', [23.4375, 30.0, 50, 72.5]), # MID LEFT
        ('magenta', [50, 30.0, 76.5625, 72.5]), # MID RIGHT
        ('white', [50, 72.5, 76.5625, 87.5]), # MID BOTTOM RIGHT
    )
    def fix_coords(coords):
        m = [w, h, w, h]
        return [int(c * .01 * m[i]) for i, c in enumerate(coords)]
    background = hex_to_rgb(colors['black'])
    image = Image.new('RGB', (w, h), background)
    draw = ImageDraw.Draw(image)
    for color, coords in rectangles:
        x1, y1, x2, y2 = fix_coords(coords)
        draw.rectangle([(x1, y1), (x2, y2)], fill=hex_to_rgb(colors[color]))
    image.save(os.path.join(template_dir, filename), 'PNG')
    return filename

Point = namedtuple('Point', ('coords', 'ct'))
Cluster = namedtuple('Cluster', ('points', 'center'))

def symlink(theme_name):
    """Set up a symlink for the new theme."""
    logger.info('Setting %s as current theme' % theme_name)
    current = os.path.join(THEMER_ROOT, 'current')
    if os.path.islink(current):
        os.unlink(current)
    os.symlink(os.path.join(THEMER_ROOT, theme_name), current)

def activate(theme_name):
    """Activate the given theme."""
    symlink(theme_name)
    dest = os.path.join(THEMER_ROOT, theme_name)
    color_file = os.path.join(dest, 'colors.yaml')
    colors = CachedColorParser(color_file).read()
    wallfix(dest, colors)
    IconUpdater(colors['primary'], colors['secondary']).update_icons()
    os.system('xrdb -merge ~/.Xresources')
    os.system('i3-msg -q restart')

def fetch_vim(color_file):
    return urllib2.urlopen('http://sweyla.com/themes/vim/sweyla%s.vim' % color_file).read()

def generate(color_source, config_file, template_dir, theme_name):
    """Generate a new theme."""
    destination = os.path.join(THEMER_ROOT, theme_name)
    wallpaper = None
    if color_source.isdigit() and not os.path.isfile(color_source):
        colors = SweylaColorParser(color_source).read()
        vim = fetch_vim(color_source)
    elif color_source.lower().endswith(('.jpg', '.png', '.jpeg')):
        colors = AutodetectColorParser(color_source).read()
        wallpaper = color_source
        vim = None
    else:
        colors = ColorParser(color_source).read()
        vim = None
    config = read_config(config_file)
    context = munge_context(config['variables'], colors)
    files = {
        key: os.path.join(destination, value)
        for key, value in config['files'].items()}
    if wallpaper:
        # Add wallpaper to the list of files to copy.
        files[wallpaper] = os.path.join(
            destination,
            'wallpaper%s' % os.path.splitext(wallpaper)[1])

    render_templates(template_dir, files, context)

    # Save a copy of the colors in the generated theme folder.
    with open(os.path.join(destination, 'colors.yaml'), 'w') as fh:
        yaml.dump(context, fh, default_flow_style=False)

    # Save the vim color scheme.
    if vim:
        logger.info('Saving vim colorscheme %s.vim' % theme_name)
        filename = os.path.join(os.environ['HOME'], '.vim/colors/%s.vim' % theme_name)
        with open(filename, 'w') as fh:
            fh.write(vim)


class ColorParser(object):
    # Colors look something like "*color0:  #FF0d3c\n"
    color_re = re.compile('.*?(color[^:]+|background|foreground):\s*(#[\da-z]{6})')

    def __init__(self, color_file):
        self.color_file = color_file
        self.colors = {}

    def mapping(self):
        return {
            'background': 'background',
            'foreground': 'foreground',
            'color0': 'black',
            'color8': 'alt_black',
            'color1': 'red',
            'color9': 'alt_red',
            'color2': 'green',
            'color10': 'alt_green',
            'color3': 'yellow',
            'color11': 'alt_yellow',
            'color4': 'blue',
            'color12': 'alt_blue',
            'color5': 'magenta',
            'color13': 'alt_magenta',
            'color6': 'cyan',
            'color14': 'alt_cyan',
            'color7': 'white',
            'color15': 'alt_white',
            'colorul': 'underline'}

    def read(self):
        color_mapping = self.mapping()

        with open(self.color_file) as fh:
            for line in fh.readlines():
                if line.startswith('!'):
                    continue
                match_obj = self.color_re.search(line.lower())
                if match_obj:
                    var, color = match_obj.groups()
                    self.colors[color_mapping[var]] = color

        if len(self.colors) < 16:
            logger.warning(
                'Error, only %s colors were read when loading color file "%s"'
                % (len(self.colors), self.color_file))
        return self.colors


class SweylaColorParser(ColorParser):
    def mapping(self):
        return {
            'bg': ['background', 'black', 'alt_black'],
            'fg': ['foreground', 'white'],
            'nf': 'red',  # name of function / method
            'nd': 'alt_red',  # decorator
            'nc': 'green',  # name of class
            'nt': 'alt_green', # ???
            'nb': 'yellow',  # e.g., "object" or "open"
            'c': 'alt_yellow',  # comments
            's': 'blue',  # string
            'mi': 'alt_blue',  # e.g., a number
            'k': 'magenta',  # e.g., "class"
            'o': 'alt_magenta', # operator, e.g "="
            'bp': 'cyan',  # e.g., "self" keyword
            'si': 'alt_cyan', # e.g. "%d"
            'se': 'alt_white',
            'support_function': 'underline'}

    def read(self):
        mapping = self.mapping()
        resp = urllib2.urlopen(
            'http://sweyla.com/themes/textfile/sweyla%s.txt' % self.color_file)
        contents = resp.read()
        for line in contents.splitlines():
            key, value = line.split(':\t')
            if key in mapping:
                colors = mapping[key]
                if not isinstance(colors, list):
                    colors = [colors]
                for color in colors:
                    self.colors[color] = value
        return self.colors

class AutodetectColorParser(ColorParser):
    def __init__(self, wallpaper_file, k=16, bg='#0e0e0e', fg='#ffffff'):
        self.wallpaper_file = wallpaper_file
        self.k = k
        self.bg = bg
        self.fg = fg

    def _get_points_from_image(self, img):
        points = []
        w, h = img.size
        for count, color in img.getcolors(w * h):
            points.append(Point(color, count))
        return points

    def get_dominant_colors(self):
        img = Image.open(self.wallpaper_file)
        img.thumbnail((300, 300))  # Resize to speed up python loop.
        width, height = img.size
        points = self._get_points_from_image(img)
        clusters = self.kmeans(points, self.k, 1)
        rgbs = [map(int, c.center.coords) for c in clusters]
        return map(rgb_to_hex, rgbs)

    def _euclidean_dist(self, p1, p2):
        return math.sqrt(
            sum((p1.coords[i] - p2.coords[i]) ** 2 for i in range(3)))

    def _calculate_center(self, points):
        vals = [0.0 for i in range(3)]
        plen = 0
        for p in points:
            plen += p.ct
            for i in range(3):
                vals[i] += (p.coords[i] * p.ct)
        return Point([(v / plen) for v in vals], 1)

    def kmeans(self, points, k, min_diff):
        clusters = [Cluster([p], p) for p in random.sample(points, k)]
        logger.info('Calculating %d dominant colors.' % k)
        while True:
            plists = [[] for i in range(k)]
            for p in points:
                smallest_distance = float('Inf')
                for i in range(k):
                    distance = self._euclidean_dist(p, clusters[i].center)
                    if distance < smallest_distance:
                        smallest_distance = distance
                        idx = i
                plists[idx].append(p)
            diff = 0
            for i in range(k):
                old = clusters[i]
                center = self._calculate_center(plists[i])
                new = Cluster(plists[i], center)
                clusters[i] = new
                diff = max(diff, self._euclidean_dist(old.center, new.center))
            logger.debug('Diff: %d' % diff)
            if diff <= min_diff:
                break
        return clusters

    def normalize(self, hexv, minv=128, maxv=256):
        r, g, b = hex_to_rgb(hexv)
        h, s, v = colorsys.rgb_to_hsv(r / 256.0, g / 256.0, b / 256.0)
        minv = minv / 256.0
        maxv = maxv / 256.0
        if v < minv:
            v = minv
        if v > maxv:
            v = maxv
        rgb = colorsys.hsv_to_rgb(h, s, v)
        return rgb_to_hex(map(lambda i: i * 256, rgb))

    def read(self):
        colors = self.get_dominant_colors()
        color_dict = {
            'background': self.bg,
            'foreground': self.fg}
        for i, color in enumerate(itertools.cycle(colors)):
            if i == 0:
                color = self.normalize(color, minv=0, maxv=32)
            elif i == 8:
                color = self.normalize(color, minv=128, maxv=192)
            elif i < 8:
                color = self.normalize(color, minv=160, maxv=224)
            else:
                color = self.normalize(color, minv=200, maxv=256)
            color_dict['color%d' % i] = color
            if i == 15:
                break
        mapping = self.mapping()
        translated = {}
        for k, v in color_dict.items():
            translated[mapping[k]] = v
        logger.debug(translated)
        return translated

class CachedColorParser(ColorParser):
    def read(self):
        with open(self.color_file) as fh:
            self.colors = yaml.load(fh)
        return self.colors


class IconUpdater(object):
    def __init__(self, primary_color, secondary_color):
        self.primary_color = primary_color
        self.secondary_color = secondary_color

    def icon_path(self):
        return os.path.join(os.environ['HOME'], '.icons/acyl')

    def primary_icon(self):
        return os.path.join(self.icon_path(), 'scalable/places/desktop.svg')

    def secondary_icon(self):
        return os.path.join(self.icon_path(), 'scalable/actions/add.svg')

    def extract_color_svg(self, filename):
        regex = re.compile('stop-color:(#[\da-zA-Z]{6})')
        with open(filename, 'r') as fh:
            for line in fh.readlines():
                match_obj = regex.search(line)
                if match_obj:
                    return match_obj.groups()[0]
        raise ValueError('Unable to determine icon color.')

    def update_icons(self):
        # Introspect a couple icon files to determine what colors are being used
        # currently.
        old_primary = self.extract_color_svg(self.primary_icon())
        old_secondary = self.extract_color_svg(self.secondary_icon())
        logger.debug('Old icon colors: %s, %s' % (old_primary, old_secondary))

        # Walk the icons, updating the colors in each svg file.
        file_count = 0
        for root, dirs, filenames in os.walk(self.icon_path()):
            for filename in filenames:
                if not filename.endswith('.svg'):
                    continue
                path = os.path.join(root, filename)
                with open(path, 'r+') as fh:
                    contents = fh.read()
                    contents = contents.replace(old_primary, self.primary_color)
                    contents = contents.replace(old_secondary, self.secondary_color)
                    fh.seek(0)
                    fh.write(contents)
                    file_count += 1
        logger.info('Checked %d icon files' % file_count)


def get_parser():
    parser = optparse.OptionParser(usage='usage: %prog [options] [list|activate|generate|current|delete] theme_name [color file]')
    parser.add_option('-t', '--template', dest='template_dir', default='i3')
    parser.add_option('-c', '--config', dest='config_file', default='config.yaml')
    parser.add_option('-a', '--activate', dest='activate', action='store_true')
    parser.add_option('-v', '--verbose', dest='verbose', action='store_true')
    parser.add_option('-d', '--debug', dest='debug', action='store_true')
    return parser

def panic(msg):
    print >> sys.stderr, msg
    sys.exit(1)

if __name__ == '__main__':
    parser = get_parser()
    options, args = parser.parse_args()

    if not args:
        panic(parser.get_usage())

    action = args[0]
    if action not in ('list', 'activate', 'generate', 'current', 'delete'):
        panic('Unknown action "%s"' % action)

    if action not in ('list', 'current') and len(args) == 1:
        panic('Missing required argument "theme_name"')
    elif action == 'list':
        themes = [
            t for t in os.listdir(THEMER_ROOT)
            if t not in ('templates', 'current')]
        print '\n'.join(sorted(themes))
        sys.exit(0)
    elif action == 'current':
        current = os.path.join(THEMER_ROOT, 'current')
        if not os.path.exists(current):
            print 'No theme'
        else:
            print os.path.basename(os.path.realpath(
                os.path.join(THEMER_ROOT, 'current')))
            os.system('colortheme')
        sys.exit(0)

    theme_name = args[1]

    # Add logging handlers.
    if options.verbose or options.debug:
        handler = logging.StreamHandler()
        logger.addHandler(handler)
    if options.debug:
        logger.setLevel(logging.DEBUG)

    if action == 'activate':
        activate(theme_name)
    elif action == 'delete':
        shutil.rmtree(os.path.join(THEMER_ROOT, theme_name))
        logger.info('Removed %s' % theme_name)
    else:
        # Find the appropriate yaml config file and load it.
        template_dir = os.path.join(TEMPLATE_ROOT, options.template_dir)
        config_file = os.path.join(template_dir, options.config_file)
        if not os.path.exists(config_file):
            panic('Unable to find file "%s"' % config_file)

        if not len(args) == 3:
            panic('Missing required color file')
        else:
            color_file = args[2]

        generate(color_file, config_file, template_dir, theme_name)

        if options.activate or raw_input('Activate now? yN ') == 'y':
            activate(theme_name)
