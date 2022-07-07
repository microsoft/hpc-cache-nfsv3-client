---
title: Customize Azure HPC Cache write-back with flush_file.py
description: How to use advanced write-back features to flush specific files from an Azure HPC Cache storage target without flushing the cached content for the entire storage target
ms.service: hpc-cache
---

# Use the flush_file.py utility for early write-back in Azure HPC Cache

This software is used with the file-specific write-back feature in Azure HPC Cache. It allows you to flush files that were  written to the Azure HPC Cache back to your NAS storage on demand, instead of waiting for them to be automatically written back from the cache.

Read [Customize file write-back in Azure HPC Cache](https://docs.microsoft.com/azure/hpc-cache/custom-flush-script) to learn more about this feature.

This article explains how to install and use the hpc-cache-nfsv3-client library with the included script flush_file.py.

## Install the software

Clone this hpc-cache-nfsv3-client repository on the client (or clients) that will use it.

This repository contains the necessary code libraries and a script named flush_file.py.

The script requires a stable Python 3 distribution to run. You have a variety of options for setting up the script and its libraries on your client machines:

* Use the 'setup.py' file included in the repository to install and configure the needed software. There are several methods to do this:

  * Use ``python3 setup.py build`` to install libraries and create an executable script in the local directory.
  * Use ``python3 setup.py install`` to install libraries and an executable script in the appropriate paths in ``/usr/local``.
  * Use ``pip install .`` from the ``hpc-cache-nfsv3-client`` directory. (Other convenient software installers also can be used.)

* Use the software included in the repository directly. Point your Python path to the downloaded repository location:

  ```bash
  export PYTHONPATH=<hpc-cache-nfsv3-client/lib>
  export PATH=<hpc-cache-nfsv3-client/bin>:$PATH
  ```

## Overview

The script 'flush_file.py' tells the HPC Cache to write specific files back to the long-term storage system.

You must stream the list of files to the script on stdin. Files can be specified individually, programmatically, or as a text document containing a newline-separated list of files. Read [Specify the files to write](#specify-the-files-to-write) for more information and examples.

```bash
$ cat *.txt | python3 flush_file.py <export_name> <server_IP>
```

Read the [Usage](#usage) section below for details about the required and optional parameters.

## Specify the files to write

``flush_file.py`` accepts one or more file paths, separated by new lines, on the standard input stream. There are a variety of ways to specify files using shell scripting. Here are some examples.

* Pass a single file name:

  ``echo "/outputdir/file1" | flush_file.py <export> <IP address>``

* Pass a list of files to write:

  ``cat flushlist | flush_file.py <export> <IP address>``
  
  In this example, "flushlist" is a text file with one file per line:

  ```
      /target1/testfile1
      /target1/output/result.txt
      /target1/output/schedule.sas
  ```

Each path specifies a single file. This utility does not support directory recursion and does not accept wildcard expressions.

Remember to specify files using their paths in the HPC Cache namespace. The flush_file.py utility creates its own connection to the HPC Cache, it doesn't use the client's mount point.

### Help with paths

To clarify which path to use in the *export* value, this table lists the various local and virtual paths for an example file.

The flush_file.py script mounts the HPC Cache system at ``/``. Even if you run flush_file.py from a client that has previously mounted the HPC Cache, the flush utility uses its own path, not the client's mount point.

| Description | File path |
|----------|-----------|
|File that you want to write: | result/output3.txt|
|File path on the compute client: | /mnt/cache/NAS_12_exp5/result/output3.txt|
|Path on the HPC Cache: | /NAS_12_exp5/result/output3.txt|
|Storage system export: | /export5|
|Path to use in flush_file.py: | /NAS_12_exp5/result/output3.txt|

## Usage

Run the ``flush_file.py`` script to trigger write-back. The script creates its own mount point to the cache and tells the cache to write the specified files to their back-end storage system right away.

The documentation here is based on the script's help file. Use the ``--help`` option to see the latest information for the software you downloaded.

This is the basic command:

  ``<stream of files to import> | flush_file.py [-h] [--threads`` *number-of-threads*``] [--timeout`` *time-in-seconds*``] [--sync] [--verbose]``*export* *server*

Supply the file or files to write with the standard input stream. Read [Specify the files to write](#specify-the-files-to-write) for details.

### Required parameters

These are positional arguments, and both are required:

* **Export** - The cache namespace path to your storage target export.

  For example, you might have a NAS storage system in your data center that holds your working set files. You create a storage target on the Azure HPC Cache to represent the on-premises system.

  When you create the storage target, you must specify a virtual namespace path for each export or subdirectory on the NAS that you want to access. If your NAS export is **/export_1**, you might use a namespace path like **/myNAS/export_1/** on the HPC Cache.

  When you want to flush files from this storage system back to the NAS, use the same namespace path, ``/myNAS/export_1/`` in the *export* term.

  To learn more about the HPC Cache aggregated namespace, read [Plan the aggregated namespace](https://docs.microsoft.com/azure/hpc-cache/hpc-cache-namespace) and [Set up the aggregated namespace - NFS namespace paths](https://docs.microsoft.com/azure/hpc-cache/add-namespace-paths?tabs=azure-portal#nfs-namespace-paths).

  Read [Help with paths](#help-with-paths), below, for more file path examples.

* **Server** - A mount IP address on the HPC Cache. You can use any valid mount IP for your cache.

  Mount addresses are listed in the cache overview page on the Azure Portal, or you can find them with the get cache commands in Azure CLI or Azure PowerShell.

### Optional parameters

These settings are optional:

* Threads - The maximum number of concurrent flush (write-back) threads. The default is four.

* Timeout - The per-file time-out value, in seconds. The default is 300 seconds.

You can use the ``--threads`` and ``--timeout`` parameters to customize the write-back behavior to accommodate your environment's capabilities. For example, writing to an on-premises data center is much slower than writing to an Azure Blob container. If you see failures or time-outs when writing to a data center, you can increase the timeout value or reduce the number of concurrent threads.

* Sync - Flush each file synchronously.

  This option means that each thread waits for one file to finish flushing before moving on to the next file. The default is to flush asynchronously, which means that a single thread issues multiple flush commands at once.

* Verbose - Report every status check for files being flushed.

  After starting a file flush, the script checks every 250 ms to see if it has completed. If the back-end storage system is slow, or if you are running a large number of threads, there can be a lot of file status checks.

  Output is written to the console; you can redirect it to a file.

### Examples

Here are some simple examples.

Synchronously flush the files in the supplied list 'flushlist' from the HPC Cache located at 203.0.113.87:

```bash
cat flushlist | python3 flush_file.py --sync /1_1_1_0 203.0.113.87 
```

Flush one particular file on the client (/testdir/testfile) to the export /1_1_1_0 from the HPC Cache with mount point 203.0.113.87:

```bash
echo /testdir/testfile | python3 bin/flush_file.py /1_1_1_0 203.0.113.87
```

## Contributing

This project welcomes contributions and suggestions. Most contributions require you to agree to a
Contributor License Agreement (CLA) declaring that you have the right to, and actually do, grant us
the rights to use your contribution. For details, visit https://cla.opensource.microsoft.com.

When you submit a pull request, a CLA bot will automatically determine whether you need to provide
a CLA and decorate the PR appropriately (e.g., status check, comment). Simply follow the instructions
provided by the bot. You will only need to do this once across all repos using our CLA.

This project has adopted the [Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
For more information see the [Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/) or
contact [opencode@microsoft.com](mailto:opencode@microsoft.com) with any additional questions or comments.

## Trademarks

This project may contain trademarks or logos for projects, products, or services. Authorized use of Microsoft trademarks or logos is subject to and must follow [Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/en-us/legal/intellectualproperty/trademarks/usage/general).
Use of Microsoft trademarks or logos in modified versions of this project must not cause confusion or imply Microsoft sponsorship.
Any use of third-party trademarks or logos are subject to those third-party's policies.
