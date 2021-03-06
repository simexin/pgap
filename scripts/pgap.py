#!/usr/bin/env python
from __future__ import print_function
import sys
min_python = (3,5)
try:
    assert(sys.version_info >= min_python)
except:
    from platform import python_version
    print("Python version", python_version(), "is too old.")
    print("Please use Python", ".".join(map(str,min_python)), "or later.")
    sys.exit()

from io import open
import argparse, atexit, json, os, re, shutil, subprocess, tarfile, platform

from urllib.parse import urlparse, urlencode
from urllib.request import urlopen, urlretrieve, Request
from urllib.error import HTTPError

verbose = False
docker = 'docker'

def is_venv():
    return (hasattr(sys, 'real_prefix') or
            (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix))

def install(packages):
    try:
        from pip._internal import main
    except ImportError:
        from pip import main
    main(['install'] + packages)

def get_docker_image(version):
    return 'ncbi/pgap:{}'.format(version)

def check_runtime_setting(settings, value, min):
    if settings[value] != 'unlimited' and settings[value] < min:
        print('WARNING: {} is less than the recommended value of {}'.format(value, min))

def check_runtime(version):
    image = get_docker_image(version)
    output = subprocess.check_output(
        [docker, 'run', '-i',
            '-v', '{}:/cwd'.format(os.getcwd()), image,
            'bash', '-c', 'df -k /cwd /tmp ; ulimit -a ; cat /proc/{meminfo,cpuinfo}'])
    output = output.decode('utf-8')
    settings = {'Docker image':image}
    for match in re.finditer(r'^(open files|max user processes|virtual memory) .* (\S+)\n', output, re.MULTILINE):
        value = match.group(2)
        if value != "unlimited":
            value = int(value)
        settings[match.group(1)] = value
    match = re.search(r'^Filesystem.*\n\S+ +\d+ +\d+ +(\d+) +\S+ +/\S*\n\S+ +\d+ +\d+ +(\d+) +\S+ +/\S*\n', output, re.MULTILINE)
    settings['work disk space (GiB)'] = round(int(match.group(1))/1024/1024, 1)
    settings['tmp disk space (GiB)'] = round(int(match.group(2))/1024/1024, 1)
    match = re.search(r'^MemTotal:\s+(\d+) kB', output, re.MULTILINE)
    settings['memory (GiB)'] = round(int(match.group(1))/1024/1024, 1)
    cpus = 0
    for match in re.finditer(r'^model name\s+:\s+(.*)\n', output, re.MULTILINE):
        cpus += 1
        settings['cpu model'] = match.group(1)
    settings['CPU cores'] = cpus
    settings['memory per CPU core (GiB)'] = round(settings['memory (GiB)']/cpus, 1)
    check_runtime_setting(settings, 'open files', 8000)
    check_runtime_setting(settings, 'max user processes', 100)
    check_runtime_setting(settings, 'work disk space (GiB)', 80)
    check_runtime_setting(settings, 'tmp disk space (GiB)', 10)
    check_runtime_setting(settings, 'memory (GiB)', 8)
    check_runtime_setting(settings, 'memory per CPU core (GiB)', 2)
    if verbose: print('Note: Essential runtime settings = {}'.format(settings))




class urlopen_progress:
    def __init__(self, url):
        self.remote_file = urlopen(url)
        total_size = 0
        try:
            total_size = self.remote_file.info().getheader('Content-Length').strip() # urllib2 method
        except AttributeError:
            total_size = self.remote_file.getheader('Content-Length', 0) # More modern method

        self.total_size = int(total_size)
        if self.total_size > 0:
            self.header = True
        else:
            self.header = False # a response doesn't always include the "Content-Length" header

        self.bytes_so_far = 0

    def read(self, n=10240):
        buffer = self.remote_file.read(n)
        if not buffer:
            sys.stdout.write('\n')
            return ''

        self.bytes_so_far += len(buffer)
        if self.header:
            percent = float(self.bytes_so_far) / self.total_size
            percent = round(percent*100, 2)
            sys.stderr.write("Downloaded %d of %d bytes (%0.2f%%)\r" % (self.bytes_so_far, self.total_size, percent))
        else:
            sys.stderr.write("Downloaded %d bytes\r" % (self.bytes_so_far))
        return buffer

def install_url(url, path):
    #with urlopen(url) as response:
    #with urlopen_progress(url) as response:
    response = urlopen_progress(url)
    with tarfile.open(mode='r|*', fileobj=response) as tar:
        tar.extractall(path=path)
#            while True:
#                item = tar.next()
#                if not item: break
#                print('- {}'.format(item.name))
#                tar.extract(item, set_attrs=False)

def install_cwl(version):
    if not os.path.exists('pgap-{}'.format(version)):
        print('Downloading PGAP Common Workflow Language (CWL) version {}'.format(version))
        install_url('https://github.com/ncbi/pgap/archive/{}.tar.gz'.format(version))




def setup(update):
    '''Determine version of PGAP.'''
    version = get_version()
    if update or not version:
        latest = get_remote_version()
        if version != latest:
            print('Updating PGAP to version {} (previous version was {})'.format(latest, version))
            install_docker(latest)
            install_data(latest)
            install_test_genomes(version)
        with open('VERSION', 'w', encoding='utf-8') as f:
            f.write(u'{}\n'.format(latest))
        version = latest
    if not version:
        raise RuntimeError('Failed to identify PGAP version')
    return version

def run(image, data_path, local_input, output, debug, report):
    #image = get_docker_image(version)

    # Create a work directory.
    os.mkdir(output)
    os.mkdir(output + '/log')

    # Run the actual workflow.
    data_dir = os.path.abspath(data_path)
    input_dir = os.path.dirname(os.path.abspath(local_input))
    input_file = '/pgap/user_input/pgap_input.yaml'

    with open(output +'/pgap_input.yaml', 'w') as f:
        with open(local_input) as i:
            shutil.copyfileobj(i, f)
        f.write(u'\n')
        f.write(u'supplemental_data: { class: Directory, location: /pgap/input }\n')
        if (report != 'none'):
            f.write(u'report_usage: {}\n'.format(report))
        f.flush()

    output_dir = os.path.abspath(output)
    yaml = output_dir + '/pgap_input.yaml'
    log_dir = output_dir + '/log'
    # cwltool --timestamps --default-container ncbi/pgap-utils:2018-12-31.build3344
    # --tmpdir-prefix ./tmpdir/ --leave-tmpdir --tmp-outdir-prefix ./tmp-outdir/
    #--copy-outputs --outdir ./outdir pgap.cwl pgap_input.yaml 2>&1 | tee cwltool.log

    cmd = [docker, 'run', '-i' ]
    if (platform.system() != "Windows"):
        cmd.extend(['--user', str(os.getuid()) + ":" + str(os.getgid())])
    cmd.extend(['--volume', '{}:/pgap/input:ro'.format(data_dir),
                '--volume', '{}:/pgap/user_input'.format(input_dir),
                '--volume', '{}:/pgap/user_input/pgap_input.yaml:ro'.format(yaml),
                '--volume', '{}:/pgap/output:rw'.format(output_dir),
                '--volume', '{}:/log/srv'.format(log_dir),
                image,
                'cwltool',
                '--outdir', '/pgap/output'])
    if debug:
        cmd.extend(['--tmpdir-prefix', '/pgap/output/tmpdir/',
                    '--leave-tmpdir',
                    '--tmp-outdir-prefix', '/pgap/output/tmp-outdir/',
                    '--copy-outputs'])
    cmd.extend(['pgap.cwl', input_file])
    subprocess.check_call(cmd)

class Setup:

    def __init__(self, args):
        self.args = args
        self.branch          = self.get_branch()
        self.repo            = self.get_repo()
        self.dir             = self.get_dir()
        self.local_version   = self.get_local_version()
        self.remote_versions = self.get_remote_versions()
        self.check_status()
        if (args.list):
            self.list_remote_versions()
            return
        self.use_version = self.get_use_version()
        if self.local_version != self.use_version:
            self.update()

    def get_branch(self):
        if (self.args.dev):
            return "dev"
        if (self.args.test):
            return "test"
        if (self.args.prod):
            return "prod"
        return ""

    def get_repo(self):
        if self.branch == "":
            return "pgap"
        return "pgap-"+self.branch

    def get_dir(self):
        if self.branch == "":
            return "."
        return "./"+self.branch

    def get_local_version(self):
        filename = self.dir + "/VERSION"
        if os.path.isfile(filename):
            with open(filename, encoding='utf-8') as f:
                self.local_version = f.read().strip()
        self.local_version = None


    def get_remote_versions(self):
        # Old system, where we checked github releases
        #response = urlopen('https://api.github.com/repos/ncbi/pgap/releases/latest')
        #latest = json.load(response)['tag_name']

        # Check docker hub
        url = 'https://registry.hub.docker.com/v1/repositories/ncbi/{}/tags'.format(self.repo)
        response = urlopen(url)
        json_resp = json.loads(response.read().decode())
        versions = []
        for i in reversed(json_resp):
            versions.append(i['name'])
        return versions

    def check_status(self):
        if self.local_version == None:
            print("The latest version of PGAP is {}, you have nothing installed locally.".format(self.get_latest_version()))
            return
        if self.local_version == self.get_latest_version():
            print("PGAP {} is up to date.".format(self.local_version))
            return
        print("The latest version of PGAP is {}, you are using version {}, please update.".format(self.get_latest_version(), self.local_version))

    def list_remote_versions(self):
        print("Available versions:")
        for i in self.remote_versions:
            print("\t", i)

    def get_latest_version(self):
        return self.remote_versions[0]

    def get_use_version(self):
        if self.args.use_version:
            return self.args.use_version
        if (self.local_version == None) or self.args.update:
            return self.get_latest_version()
        return self.local_version

    def update(self):
        self.install_docker()
        self.install_data()
        self.install_test_genomes()
        self.write_version()

    def install_docker(self):
        self.docker_image = "ncbi/{}:{}".format(self.repo, self.use_version)
        print('Downloading (as needed) Docker image {}'.format(self.docker_image))
        subprocess.check_call([docker, 'pull', self.docker_image])

    def install_data(self):
        self.data_path = '{}/input-{}'.format(self.dir, self.use_version)
        if not os.path.exists(self.data_path):
            print('Downloading PGAP reference data version {}'.format(self.use_version))
            suffix = ""
            if self.branch != "":
                suffix = self.branch + "."
            remote_path = 'https://s3.amazonaws.com/pgap/input-{}.{}tgz'.format(self.use_version, suffix)
            install_url(remote_path, self.dir)

    def install_test_genomes(self):
        local_path = "{}/test_genomes".format(self.dir)
        if not os.path.exists(local_path):
            print('Downloading PGAP test genomes')
            install_url('https://s3.amazonaws.com/pgap-data/test_genomes.tgz', self.dir)

    def write_version(self):
        filename = self.dir + "/VERSION"
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(u'{}\n'.format(self.use_version))

        
def main():
    parser = argparse.ArgumentParser(description='Run PGAP.')
    parser.add_argument('input', nargs='?',
                        help='Input YAML file to process.')
    parser.add_argument('-V', '--version', action='store_true',
                        help='Print currently set up PGAP version')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Verbose mode')

    version_group = parser.add_mutually_exclusive_group()
    version_group.add_argument('--dev',  action='store_true', help="Set development mode")
    version_group.add_argument('--test', action='store_true', help="Set test mode")
    version_group.add_argument('--prod', action='store_true', help="Set production mode")

    action_group = parser.add_mutually_exclusive_group()
    action_group.add_argument('-l', '--list', action='store_true', help='List available versions.')
    action_group.add_argument('-u', '--update', dest='update', action='store_true',
                              help='Update to the latest PGAP version, including reference data.')
    action_group.add_argument('--use-version', dest='use_version', help=argparse.SUPPRESS)

    report_group = parser.add_mutually_exclusive_group()
    report_group.add_argument('-r', '--report-usage-true', dest='report_usage_true', action='store_true',
                        help='Set the report_usage flag in the YAML to true.')
    report_group.add_argument('-n', '--report-usage-false', dest='report_usage_false', action='store_true',
                        help='Set the report_usage flag in the YAML to false.')

    parser.add_argument('-d', '--docker', metavar='path', default='docker',
                        help='Docker executable, which may include a full path like /usr/bin/docker')
    parser.add_argument('-o', '--output', metavar='path', default='output',
                        help='Output directory to be created, which may include a full path')
    parser.add_argument('-t', '--test-genome', dest='test_genome', action='store_true',
                        help='Run a test genome')
    parser.add_argument('-D', '--debug', action='store_true',
                        help='Debug mode')
    args = parser.parse_args()
    s = Setup(args)
    #check_runtime(version)

    if args.test_genome:
        input_file = s.dir + '/test_genomes/MG37/input.yaml'
    else:
        input_file = args.input

    report='none'
    if (args.report_usage_true):
        report = 'true'
    if (args.report_usage_false):
        report = 'false'

    if input_file:
        run(s.docker_image, s.data_path, input_file, args.output, args.debug, report)

    sys.exit()

    verbose = args.verbose
    docker = args.docker
    debug = args.debug

    repo = get_repo(args)

    if (args.version):
        version = get_version()
        if version:
            print('PGAP version {}'.format())
        else:
            print('PGAP not installed; use --update to install the latest version.')
            exit(0)

    version = setup(args.update)


        
    if input:
        #run(version, input, args.output, debug, report)
        pass
    
if __name__== "__main__":
    main()
