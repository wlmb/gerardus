# Copyright Dave Abrahams 2012. Distributed under the Boost
# Software License, Version 1.0. (See accompanying
# file LICENSE_1_0.txt or copy at http://www.boost.org/LICENSE_1_0.txt)
import sys
import os

from ryppl.support.path import *
from ryppl.support._argparse import valid_0install_feed, creatable_path
import zeroinstall.injector.requirements
import zeroinstall.injector.config
import zeroinstall.injector.driver
from subprocess import check_call,PIPE
from ryppl.support import executable_path
from ryppl.support.threadpool import ThreadPool
from ryppl.support._zeroinstall.sloppy_cache import SloppyCache
from ryppl.support import cmake
import logging

def command_line_interface(cli):
    '''Set up a project workspace for the given feeds'''

    import zeroinstall.injector.model
    cli.add_argument(
        '--refresh'
        , action='store_true'
        , help='Force 0install to update its cached feeds now')

    cli.add_argument(
        'feed'
        , nargs = '+'
        , type=valid_0install_feed
        , help='0install feed of Ryppl project to develop')

    cli.add_argument(
        'workspace'
        , nargs=1
        , type=creatable_path
        , help='Path to project workspace directory, which must not already exist')

_git = executable_path('git')

def solve(args, config):
    selections = None
    versions = {}
    for iface_uri in args.feed:
        requirements = zeroinstall.injector.requirements.Requirements(iface_uri)
        requirements.command = 'develop'
        
	driver = zeroinstall.injector.driver.Driver(
            config=config, requirements=requirements)

        refresh = args.refresh
        if not refresh:
            # Note that need_download() triggers a solve
            driver.need_download()
            refresh = any(
                feed for feed in driver.solver.feeds_used if
                # Ignore (memory-only) PackageKit feeds
                not feed.startswith('distribution:') and
                config.iface_cache.is_stale(feed, config.freshness))

        if refresh: 
            print 'Fetching stale/missing 0install feeds'

        blocker = driver.solve_with_downloads(refresh)
        if blocker:
            zeroinstall.support.tasks.wait_for_blocker(blocker)

        if not driver.solver.ready:
            raise driver.solver.get_failure_reason()

        if not selections:
            selections = driver.solver.selections
        else:
            for uri,sel in driver.solver.selections.selections.items():
                v = versions.setdefault(uri, sel.attrs['version'])
                assert v == sel.attrs['version'], 'Version mismatch; not yet supported.'
                selections.selections[uri] = sel
    return selections

def git_add_feed_submodule(feed, tree_ish, where, id, config, tasks, progress):
    print '    ' + feed.get_name()
    repos = [
        x for x in feed.metadata 
        if x.uri == 'http://ryppl.org/2012' and x.name == 'vcs-repository'
        ]
    if len(repos) == 0:
        return None
    assert len(repos) == 1

    repo = repos[0]
    submodule_name = repo.attrs['href'].rsplit('/',1)[-1].rsplit('.',1)[0]
    work_dir = where/submodule_name
    os.makedirs(work_dir)
    check_call([_git, 'init', '-q'], cwd=work_dir)
    check_call([_git, 'submodule', '-q', 'add', repo.attrs['href'], 
                work_dir], stdout=PIPE, stderr=PIPE)

    tasks.add_task(git_add_feed_submodule2, feed, repo, work_dir, id, submodule_name, progress)
    return submodule_name

def git_add_feed_submodule2(feed, repo, work_dir, id, submodule_name, progress):
    check_call([_git, 'remote', 'add', 'origin', repo.attrs['href']]
               , cwd=work_dir)

    implementation = feed.implementations[id]
    tree_ish = implementation.metadata['http://ryppl.org/2012 vcs-revision']
    check_call([_git, 'fetch', '-q', 'origin'], cwd=work_dir)
    check_call([_git, 'checkout', '-q', tree_ish], cwd=work_dir)
    if len(progress):
        print '  %s: done.' % submodule_name
    sys.stdout.flush()

cmakelists_head = '''# Project file generated by Ryppl
cmake_minimum_required(VERSION 2.8.8 FATAL_ERROR)
'''

push_disable_tests_docs_examples = '''
foreach(name TEST DOC EXAMPLE)
  set_property(DIRECTORY PROPERTY RYPPL_DISABLE_${name}S ${RYPPL_DISABLE_${name}S})
  set(RYPPL_DISABLE_${name}S true)
endforeach()
'''

pop_disable_tests_docs_examples = '''
foreach(name TEST DOC EXAMPLE)
  get_property(RYPPL_DISABLE_${name}S DIRECTORY PROPERTY RYPPL_DISABLE_${name}S)
endforeach()
'''

def prepare_src(src, args, selections, config):
    print 'Creating src/ directory...'
    dependency_subdir = Path('.dependencies')
    os.makedirs(src/dependency_subdir)
    os.chdir(src)
    check_call([_git, 'init', '-q'])

    top_cmakelists_txt = open(curdir/'CMakeLists.txt','w')
    dep_cmakelists_txt = open(dependency_subdir/'CMakeLists.txt','w')

    for f in (top_cmakelists_txt, dep_cmakelists_txt): 
        f.write(cmakelists_head)
    
    top_cmakelists_txt.write('''
add_definitions(-DBOOST_ALL_NO_LIB)
list(APPEND CMAKE_MODULE_PATH "${CMAKE_CURRENT_LIST_DIR}/%s/ryppl/cmake/Modules")
set(RYPPL_INITIAL_PASS TRUE CACHE BOOL "")
''' % dependency_subdir)
    dep_cmakelists_txt.write(push_disable_tests_docs_examples)

    tasks = ThreadPool(8)

    progress = []

    submodules = {}
    print '  Fetching components:'
    for uri,sel in selections.selections.items():

        feed = config.iface_cache.get_feed(uri)
        requested = uri in args.feed
        parent_dir = Path(curdir if requested else dependency_subdir)

        if feed.implementations.get(sel.id):
            submodule = git_add_feed_submodule(
                feed
                , sel.attrs['version']
                , parent_dir
                , sel.id
                , config
                , tasks
                , progress
                )
            if submodule:
                submodules[submodule] = (requested, parent_dir/submodule)

    print
    print 'Waiting for submodules...'
    sys.stdout.flush()
    progress.append(True) # Give some feedback while we wait
    tasks.wait_completion()
    print 'done.'

    for submodule, (requested,dir) in sorted(submodules.items()):
        if os.path.isfile(dir/'CMakeLists.txt'):
            (top_cmakelists_txt if requested else dep_cmakelists_txt).write(
                'add_subdirectory(%s)\n' % submodule)

    dep_cmakelists_txt.write(pop_disable_tests_docs_examples)
    top_cmakelists_txt.write('''
add_subdirectory(%s)

if(RYPPL_INITIAL_PASS)
  # report an error in order to inhibit the generation step (save time).
  message(SEND_ERROR
    "Initial pass successfully completed, now run again!"
    )
  set(RYPPL_INITIAL_PASS FALSE CACHE BOOL "" FORCE)
endif(RYPPL_INITIAL_PASS)
''' % dependency_subdir)

    check_call([_git, 'add', '-A'])
    check_call([_git, 'commit', '-q', '-m', 'initial workspace setup'])

def prepare_build(build_dir):
    os.makedirs(build_dir)

    # Have the user select a cmake generator
    generators = cmake.generators()
    n = 0
    if logging.getLogger().getEffectiveLevel() < logging.ERROR \
    and (not hasattr(sys.stdin, 'isatty') or sys.stdin.isatty()):
        print 'Please select a build system:'
        for i,g in enumerate(generators):
            print '[%d] %s' % (i, g)

        while True:
            sys.stdout.write('Build system [0-%d]:' % (len(generators) - 1))
            sys.stdout.flush()
            l = sys.stdin.readline()
            try: n = int(l.strip())
            except: continue
            if n < 0 or n >= len(generators): continue
            break

        cmake.configure_for_circular_dependencies(
            '-G', generators[n], '../src'
            , cwd=build_dir)

def run(args):
    # Suppress all 0install GUI elements
    os.environ['DISPLAY']=''

    config = zeroinstall.injector.config.load_config()
    config._iface_cache = SloppyCache()

    # Only download new feed information every day unless otherwise
    # specified.  NOTE: Values lower than one hour will be ignored
    # unless you also monkeypatch
    # zeroinstall.injector.iface_cache.FAILED_CHECK_DELAY
    config.freshness = 60*60*24
    
    selections = solve(args, config)

    workspace = Path(args.workspace[0]).abspath

    prepare_src(workspace/'src', args, selections, config)
    prepare_build(workspace/'build')